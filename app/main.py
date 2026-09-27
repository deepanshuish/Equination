"""FastAPI application: JSON API + static single-page UI."""
from __future__ import annotations

import base64
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, scheduler
from .backtest import run_backtest
from .config import NIFTY_KEY, PASSWORD, STATIC_DIR
from .scanner import refresh_instruments, start_scan_async, state
from .strategy import StrategyParams
from .universe import select_universe
from .upstox_client import AuthError, UpstoxError, auth_dialog_url, exchange_code, verify_token

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("equination")

SECRET_KEYS = {"api_secret", "access_token"}
EDITABLE_KEYS = {
    "api_key", "api_secret", "redirect_uri", "universe", "capital", "risk_per_trade_pct", "top_n",
    "min_price", "min_turnover_cr", "schedule_time", "schedule_enabled",
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
    if vals.get("api_secret", None) == "":
        vals.pop("api_secret")
    if "schedule_time" in vals:
        try:
            hh, mm = vals["schedule_time"].split(":")
            assert 0 <= int(hh) < 24 and 0 <= int(mm) < 60
        except (ValueError, AssertionError):
            raise HTTPException(400, "schedule_time must be HH:MM")
    for k in ("capital", "risk_per_trade_pct", "top_n", "min_price", "min_turnover_cr"):
        if k in vals:
            try:
                float(vals[k])
            except ValueError:
                raise HTTPException(400, f"{k} must be a number")
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
    params = StrategyParams.from_settings(settings)
    instruments = db.get_instruments()
    universe, _ = select_universe(instruments, settings["universe"])
    keys = universe["instrument_key"].tolist()
    if not keys:
        return {"error": "No instruments cached yet - run a scan first."}
    candles = db.load_candles([NIFTY_KEY, *keys])
    nifty = candles[candles["instrument_key"] == NIFTY_KEY].set_index("date")["close"]
    if nifty.empty:
        return {"error": "No Nifty history cached yet - run a scan first."}
    return run_backtest(candles[candles["instrument_key"] != NIFTY_KEY], nifty, params)


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
