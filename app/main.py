"""FastAPI application: JSON API + static single-page UI."""
from __future__ import annotations

import base64
import json
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, fundamentals as fnd, performance, scheduler, sentiment as snt, swing
from .backtest import run_backtest
from .config import NIFTY_KEY, PASSWORD, STATIC_DIR
from .scanner import refresh_instruments, start_scan_async, start_sweep_async, state
from .strategy import StrategyParams
from .universe import select_universe
from .upstox_client import AuthError, UpstoxError, auth_dialog_url, exchange_code, verify_token

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("equination")

SECRET_KEYS = {"api_secret", "access_token", "marketaux_key"}
EDITABLE_KEYS = {
    "api_key", "api_secret", "redirect_uri", "universe", "capital", "risk_per_trade_pct", "top_n",
    "min_price", "min_turnover_cr", "schedule_time", "schedule_enabled", "mode", "hold_days",
    "require_fundamentals", "exclude_symbols", "marketaux_key", "sentiment_days", "w_quant", "w_sentiment", "w_quality",
    "sweep_budget", "sweep_in_scan",
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Equination", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Optional password gate for deployed instances (EQUINATION_PASSWORD)."""
    if PASSWORD and not request.url.path.startswith("/static"):
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(pw, PASSWORD)
            except (ValueError, UnicodeDecodeError):
                ok = False
        if not ok:
            return Response("Authentication required", 401, headers={"WWW-Authenticate": 'Basic realm="Equination"'})
    return await call_next(request)


def _public_settings() -> dict:
    s = db.get_settings()
    out = {k: v for k, v in s.items() if k not in SECRET_KEYS}
    out["has_api_secret"] = bool(s.get("api_secret"))
    out["has_access_token"] = bool(s.get("access_token"))
    out["has_marketaux_key"] = bool(s.get("marketaux_key"))
    out["token_issued_at"] = s.get("token_issued_at", "")
    out["schedule"] = scheduler.describe()
    return out


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


# ---------------------------------------------------------------- settings
class SettingsIn(BaseModel):
    values: dict[str, str]


@app.get("/api/settings")
def get_settings():
    return _public_settings()


@app.post("/api/settings")
def save_settings(body: SettingsIn):
    vals = {k: v for k, v in body.values.items() if k in EDITABLE_KEYS}
    # An empty secret from the form means "keep the stored one".
    for k in ("api_secret", "marketaux_key"):
        if vals.get(k, None) == "":
            vals.pop(k)
    for k in ("sentiment_days", "w_quant", "w_sentiment", "w_quality", "sweep_budget"):
        if k in vals:
            try:
                float(vals[k])
            except ValueError:
                raise HTTPException(400, f"{k} must be a number")
    if "schedule_time" in vals:
        try:
            hh, mm = vals["schedule_time"].split(":")
            assert 0 <= int(hh) < 24 and 0 <= int(mm) < 60
        except (ValueError, AssertionError):
            raise HTTPException(400, "schedule_time must be HH:MM")
    for k in ("capital", "risk_per_trade_pct", "top_n", "min_price", "min_turnover_cr", "hold_days"):
        if k in vals:
            try:
                float(vals[k])
            except ValueError:
                raise HTTPException(400, f"{k} must be a number")
    if vals.get("mode") not in (None, "swing", "positional"):
        raise HTTPException(400, "mode must be swing or positional")
    db.set_settings(vals)
    scheduler.apply_schedule()
    return _public_settings()


class TokenIn(BaseModel):
    access_token: str


@app.post("/api/token")
def set_token(body: TokenIn):
    """Manual path for users who paste a token generated elsewhere."""
    token = body.access_token.strip()
    if not token:
        raise HTTPException(400, "Empty token")
    try:
        profile = verify_token(token)
    except AuthError as e:
        raise HTTPException(401, str(e))
    except UpstoxError as e:
        raise HTTPException(502, str(e))
    db.set_settings({"access_token": token, "token_issued_at": db.now_iso()})
    return {"ok": True, "user": profile.get("user_name") or profile.get("email")}


@app.post("/api/marketaux/clear")
def clear_marketaux():
    db.set_settings({"marketaux_key": ""})
    return {"ok": True}


@app.get("/api/sentiment/{symbol}")
def sentiment_debug(symbol: str, refresh: bool = False):
    """Parsed sentiment plus the raw Marketaux articles, for checking the mapping."""
    inst = db.get_instruments()
    row = inst[inst["symbol"].str.upper() == symbol.upper()]
    name = str(row.iloc[0]["name"] or "") if not row.empty else symbol
    key = db.get_settings().get("marketaux_key", "")
    try:
        s = snt.fetch(symbol.upper(), name, key) if (refresh or snt.get_cached(symbol.upper()) is None) and key else snt.get_cached(symbol.upper(), 1e6)
    except snt.SentimentAuthError as e:
        raise HTTPException(401, str(e))
    except snt.SentimentError as e:
        raise HTTPException(502, str(e))
    if s is None:
        raise HTTPException(404, "no cached sentiment and no Marketaux key to fetch it")
    raw = db.load_news(symbol.upper()) or {}
    return {"symbol": symbol.upper(), "sentiment": s.to_dict(), "points": snt.sentiment_points(s),
            "raw": json.loads(raw["raw"]) if raw.get("raw") else None}


@app.post("/api/sweep")
def sweep():
    if not db.get_settings().get("marketaux_key"):
        raise HTTPException(400, "No Marketaux API key configured")
    try:
        start_sweep_async()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"started": True}


@app.get("/api/sweep/latest")
def sweep_latest(top: int = 5):
    res = db.latest_sweep()
    if not res:
        return {"status": "none"}
    return {**{k: v for k, v in res.items() if k != "all"}, "top": res["all"][:top],
            "bottom": res["all"][-top:][::-1] if len(res["all"]) > top else []}


@app.get("/api/performance")
def performance_report():
    return performance.grade_all()


@app.post("/api/token/clear")
def clear_token():
    db.set_settings({"access_token": "", "token_issued_at": ""})
    return {"ok": True}


# ------------------------------------------------------------------- oauth
@app.get("/login")
def login():
    s = db.get_settings()
    if not s.get("api_key"):
        raise HTTPException(400, "Save your Upstox API key first")
    return RedirectResponse(auth_dialog_url(s["api_key"], s["redirect_uri"]))


@app.get("/callback")
def callback(request: Request):
    code = request.query_params.get("code")
    if not code:
        return RedirectResponse("/?login=error&msg=" + (request.query_params.get("error") or "no_code"))
    s = db.get_settings()
    try:
        token = exchange_code(s["api_key"], s["api_secret"], s["redirect_uri"], code)
    except AuthError as e:
        log.warning("token exchange failed: %s", e)
        return RedirectResponse("/?login=error&msg=token_exchange_failed")
    db.set_settings({"access_token": token, "token_issued_at": db.now_iso()})
    return RedirectResponse("/?login=ok")


# -------------------------------------------------------------------- scan
@app.post("/api/scan")
def scan():
    try:
        start_scan_async("manual")
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"started": True}


@app.get("/api/scan/status")
def scan_status():
    return state.snapshot()


@app.get("/api/scan/latest")
def latest_scan():
    scan = db.get_scan()
    return scan or {"results": [], "status": "none"}


@app.get("/api/scans")
def scans():
    return db.list_scans()


@app.get("/api/scans/{scan_id}")
def scan_detail(scan_id: int):
    scan = db.get_scan(scan_id)
    if not scan:
        raise HTTPException(404, "scan not found")
    return scan


# ---------------------------------------------------------------- backtest
@app.post("/api/backtest")
def backtest():
    settings = db.get_settings()
    instruments = db.get_instruments()
    universe, _ = select_universe(instruments, settings["universe"])
    keys = universe["instrument_key"].tolist()
    if not keys:
        return {"error": "No instruments cached yet - run a scan first."}
    candles = db.load_candles([NIFTY_KEY, *keys])
    nifty = candles[candles["instrument_key"] == NIFTY_KEY].set_index("date")["close"]
    if nifty.empty:
        return {"error": "No Nifty history cached yet - run a scan first."}
    stocks = candles[candles["instrument_key"] != NIFTY_KEY]
    mode = settings.get("mode", "swing")
    if mode == "swing":
        res = swing.backtest(stocks, nifty, swing.SwingParams.from_settings(settings))
    else:
        res = run_backtest(stocks, nifty, StrategyParams.from_settings(settings))
    res["mode"] = mode
    return res


@app.get("/api/fundamentals/{symbol}")
def fundamentals_debug(symbol: str, refresh: bool = False):
    """Normalised fundamentals plus the raw Upstox payload, for checking the field mapping."""
    inst = db.get_instruments()
    row = inst[inst["symbol"].str.upper() == symbol.upper()]
    if row.empty:
        raise HTTPException(404, "symbol not in the cached instrument master - run a scan first")
    isin, name = row.iloc[0]["isin"], row.iloc[0]["name"]
    token = db.get_settings().get("access_token", "")
    try:
        f = fnd.fetch(isin, token, name or "") if (refresh or fnd.get_cached(isin) is None) and token else fnd.get_cached(isin)
    except AuthError as e:
        raise HTTPException(401, str(e))
    if f is None:
        raise HTTPException(404, "no cached fundamentals and no access token to fetch them")
    raw = db.load_fundamentals(isin) or {}
    return {"symbol": symbol.upper(), "isin": isin, "fundamentals": f.to_dict(),
            "assessment": fnd.assess(f, fnd.QualityParams()), "raw": json.loads(raw["raw"]) if raw.get("raw") else None}


# ------------------------------------------------------------------ status
@app.get("/api/status")
def status():
    counts = db.candle_counts()
    inst = db.get_instruments()
    return {
        "instruments": int(len(inst)),
        "symbols_with_data": len(counts),
        "candle_rows": int(sum(counts.values())),
        "scan": state.snapshot(),
        "schedule": scheduler.describe(),
    }


@app.post("/api/instruments/refresh")
def instruments_refresh():
    try:
        inst = refresh_instruments(force=True)
    except UpstoxError as e:
        raise HTTPException(502, str(e))
    return {"instruments": int(len(inst))}
