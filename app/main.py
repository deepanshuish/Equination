"""FastAPI application: public landing + auth pages, per-user JSON API, app shell."""
from __future__ import annotations

import json
import logging
import os
import re
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, db, fundamentals as fnd, performance, scheduler, sentiment as snt, swing
from .backtest import run_backtest
from .config import NIFTY_KEY, PUBLIC_URL, STATIC_DIR
from .scanner import refresh_instruments, start_scan_async, start_sweep_async, state_for
from .strategy import StrategyParams
from .universe import select_universe
from .upstox_client import AuthError, UpstoxError, auth_dialog_url, exchange_code, verify_token

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("equination")

EDITABLE_KEYS = {
    "api_key", "api_secret", "redirect_uri", "universe", "capital", "risk_per_trade_pct", "top_n",
    "min_price", "min_turnover_cr", "schedule_time", "schedule_enabled", "mode", "hold_days",
    "require_fundamentals", "exclude_symbols", "marketaux_key", "sentiment_days", "w_quant", "w_sentiment", "w_quality",
    "sweep_budget", "sweep_in_scan",
}
NUMERIC_KEYS = ("capital", "risk_per_trade_pct", "top_n", "min_price", "min_turnover_cr", "hold_days",
                "sentiment_days", "w_quant", "w_sentiment", "w_quality", "sweep_budget")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Equination", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(HTTPException)
async def _http_exc(request: Request, exc: HTTPException):
    # API callers get JSON; page requests that need auth are sent to the login page.
    if exc.status_code == 401 and not request.url.path.startswith("/api/"):
        return RedirectResponse(f"/login?next={request.url.path}", status_code=302)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


def _public_settings(user_id: int) -> dict:
    s = db.get_settings(user_id)
    out = {k: v for k, v in s.items() if k not in auth.SECRET_SETTINGS}
    out["has_api_secret"] = bool(s.get("api_secret"))
    out["has_access_token"] = bool(s.get("access_token"))
    out["has_marketaux_key"] = bool(s.get("marketaux_key"))
    out["token_issued_at"] = s.get("token_issued_at", "")
    out["schedule"] = scheduler.describe(user_id)
    out["suggested_redirect_uri"] = f"{PUBLIC_URL}/callback"
    return out


# ------------------------------------------------------------------- pages
@app.get("/")
def landing(request: Request):
    if auth.user_from_request(request):
        return RedirectResponse("/app")
    return FileResponse(STATIC_DIR / "landing.html")


@app.get("/app")
def app_page(user: dict = Depends(auth.require_user)):
    return FileResponse(STATIC_DIR / "app.html")


@app.get("/login")
@app.get("/signup")
def auth_page(request: Request):
    if auth.user_from_request(request):
        return RedirectResponse("/app")
    return FileResponse(STATIC_DIR / "auth.html")


@app.get("/legal")
def legal_page():
    return FileResponse(STATIC_DIR / "legal.html")


# -------------------------------------------------------------------- auth
@app.get("/api/auth/config")
def auth_config():
    return {"signup_allowed": auth.signup_allowed(), "invite_required": bool(os.environ.get("EQUINATION_INVITE_CODE"))}


@app.post("/api/auth/signup")
def signup(email: str = Form(...), password: str = Form(...), invite: str = Form("")):
    if not auth.signup_allowed():
        raise HTTPException(403, "Sign-ups are closed right now")
    if not auth.invite_ok(invite):
        raise HTTPException(403, "Invalid invite code")
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "Enter a valid email address")
    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if db.get_user_by_email(email):
        raise HTTPException(409, "An account with that email already exists")
    first = db.user_count() == 0
    uid = db.create_user(email, auth.hash_password(password))
    if first:
        db.adopt_legacy_data(uid)  # keys and scans from the single-user version, if any
    db.set_settings(uid, {"redirect_uri": f"{PUBLIC_URL}/callback"} if not db.get_settings(uid).get("api_key") else {})
    scheduler.apply_schedule(uid)
    token = auth.create_session(uid)
    resp = JSONResponse({"ok": True, "user": {"id": uid, "email": email}})
    resp.set_cookie(auth.SESSION_COOKIE, token, **auth.cookie_kwargs())
    return resp


@app.post("/api/auth/login")
def login_api(email: str = Form(...), password: str = Form(...)):
    user = db.get_user_by_email(email)
    if not user or not auth.verify_password(password, user["password_hash"]):
        raise HTTPException(401, "Wrong email or password")
    token = auth.create_session(user["id"])
    resp = JSONResponse({"ok": True, "user": {"id": user["id"], "email": user["email"]}})
    resp.set_cookie(auth.SESSION_COOKIE, token, **auth.cookie_kwargs())
    return resp


@app.post("/api/auth/logout")
@app.get("/logout")
def logout(request: Request):
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        db.delete_session(token)
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@app.get("/api/me")
def me(user: dict = Depends(auth.require_user)):
    return {"id": user["id"], "email": user["email"], "created_at": user["created_at"]}


# ---------------------------------------------------------------- settings
class SettingsIn(BaseModel):
    values: dict[str, str]


@app.get("/api/settings")
def get_settings(user: dict = Depends(auth.require_user)):
    return _public_settings(user["id"])


@app.post("/api/settings")
def save_settings(body: SettingsIn, user: dict = Depends(auth.require_user)):
    vals = {k: v for k, v in body.values.items() if k in EDITABLE_KEYS}
    for k in auth.SECRET_SETTINGS:  # an empty secret from the form means "keep the stored one"
        if vals.get(k, None) == "":
            vals.pop(k)
    if "schedule_time" in vals:
        try:
            hh, mm = vals["schedule_time"].split(":")
            assert 0 <= int(hh) < 24 and 0 <= int(mm) < 60
        except (ValueError, AssertionError):
            raise HTTPException(400, "schedule_time must be HH:MM")
    for k in NUMERIC_KEYS:
        if k in vals:
            try:
                float(vals[k])
            except ValueError:
                raise HTTPException(400, f"{k} must be a number")
    if vals.get("mode") not in (None, "swing", "positional"):
        raise HTTPException(400, "mode must be swing or positional")
    db.set_settings(user["id"], vals)
    scheduler.apply_schedule(user["id"])
    return _public_settings(user["id"])


class TokenIn(BaseModel):
    access_token: str


@app.post("/api/token")
def set_token(body: TokenIn, user: dict = Depends(auth.require_user)):
    token = body.access_token.strip()
    if not token:
        raise HTTPException(400, "Empty token")
    try:
        profile = verify_token(token)
    except AuthError as e:
        raise HTTPException(401, str(e))
    except UpstoxError as e:
        raise HTTPException(502, str(e))
    db.set_settings(user["id"], {"access_token": token, "token_issued_at": db.now_iso()})
    return {"ok": True, "user": profile.get("user_name") or profile.get("email")}


@app.post("/api/token/clear")
def clear_token(user: dict = Depends(auth.require_user)):
    db.set_settings(user["id"], {"access_token": "", "token_issued_at": ""})
    return {"ok": True}


@app.post("/api/marketaux/clear")
def clear_marketaux(user: dict = Depends(auth.require_user)):
    db.set_settings(user["id"], {"marketaux_key": ""})
    return {"ok": True}


# ------------------------------------------------------------------- oauth
@app.get("/upstox/login")
def upstox_login(user: dict = Depends(auth.require_user)):
    s = db.get_settings(user["id"])
    if not s.get("api_key"):
        raise HTTPException(400, "Save your Upstox API key first")
    return RedirectResponse(auth_dialog_url(s["api_key"], s["redirect_uri"]))


@app.get("/callback")
def callback(request: Request, user: dict = Depends(auth.require_user)):
    code = request.query_params.get("code")
    if not code:
        return RedirectResponse("/app?login=error&msg=" + (request.query_params.get("error") or "no_code"))
    s = db.get_settings(user["id"])
    try:
        token = exchange_code(s["api_key"], s["api_secret"], s["redirect_uri"], code)
    except AuthError as e:
        log.warning("token exchange failed for user %s: %s", user["id"], e)
        return RedirectResponse("/app?login=error&msg=token_exchange_failed")
    db.set_settings(user["id"], {"access_token": token, "token_issued_at": db.now_iso()})
    return RedirectResponse("/app?login=ok")


# -------------------------------------------------------------------- scan
@app.post("/api/scan")
def scan(user: dict = Depends(auth.require_user)):
    try:
        start_scan_async(user["id"], "manual")
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"started": True}


@app.get("/api/scan/status")
def scan_status(user: dict = Depends(auth.require_user)):
    return state_for(user["id"]).snapshot()


@app.get("/api/scan/latest")
def latest_scan(user: dict = Depends(auth.require_user)):
    scan = db.get_scan(user["id"])
    return scan or {"results": [], "status": "none"}


@app.get("/api/scans")
def scans(user: dict = Depends(auth.require_user)):
    return db.list_scans(user["id"])


@app.get("/api/scans/{scan_id}")
def scan_detail(scan_id: int, user: dict = Depends(auth.require_user)):
    scan = db.get_scan(user["id"], scan_id)
    if not scan:
        raise HTTPException(404, "scan not found")
    return scan


@app.post("/api/sweep")
def sweep(user: dict = Depends(auth.require_user)):
    if not db.get_settings(user["id"]).get("marketaux_key"):
        raise HTTPException(400, "No Marketaux API key configured")
    try:
        start_sweep_async(user["id"])
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"started": True}


@app.get("/api/sweep/latest")
def sweep_latest(top: int = 5, user: dict = Depends(auth.require_user)):
    res = db.latest_sweep(user["id"])
    if not res:
        return {"status": "none"}
    return {**{k: v for k, v in res.items() if k != "all"}, "top": res["all"][:top],
            "bottom": res["all"][-top:][::-1] if len(res["all"]) > top else []}


@app.get("/api/performance")
def performance_report(user: dict = Depends(auth.require_user)):
    return performance.grade_all(user["id"])


# ---------------------------------------------------------------- backtest
@app.post("/api/backtest")
def backtest(user: dict = Depends(auth.require_user)):
    settings = db.get_settings(user["id"])
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


# ------------------------------------------------------------------- debug
@app.get("/api/fundamentals/{symbol}")
def fundamentals_debug(symbol: str, refresh: bool = False, user: dict = Depends(auth.require_user)):
    inst = db.get_instruments()
    row = inst[inst["symbol"].str.upper() == symbol.upper()]
    if row.empty:
        raise HTTPException(404, "symbol not in the cached instrument master - run a scan first")
    isin, name = row.iloc[0]["isin"], row.iloc[0]["name"]
    token = db.get_settings(user["id"]).get("access_token", "")
    try:
        f = fnd.fetch(isin, token, name or "") if (refresh or fnd.get_cached(isin) is None) and token else fnd.get_cached(isin)
    except AuthError as e:
        raise HTTPException(401, str(e))
    if f is None:
        raise HTTPException(404, "no cached fundamentals and no access token to fetch them")
    raw = db.load_fundamentals(isin) or {}
    return {"symbol": symbol.upper(), "isin": isin, "fundamentals": f.to_dict(),
            "assessment": fnd.assess(f, fnd.QualityParams()), "raw": json.loads(raw["raw"]) if raw.get("raw") else None}


@app.get("/api/sentiment/{symbol}")
def sentiment_debug(symbol: str, refresh: bool = False, user: dict = Depends(auth.require_user)):
    inst = db.get_instruments()
    row = inst[inst["symbol"].str.upper() == symbol.upper()]
    name = str(row.iloc[0]["name"] or "") if not row.empty else symbol
    key = db.get_settings(user["id"]).get("marketaux_key", "")
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


@app.get("/api/status")
def status(user: dict = Depends(auth.require_user)):
    counts = db.candle_counts()
    inst = db.get_instruments()
    return {
        "instruments": int(len(inst)), "symbols_with_data": len(counts), "candle_rows": int(sum(counts.values())),
        "scan": state_for(user["id"]).snapshot(), "schedule": scheduler.describe(user["id"]),
    }


@app.post("/api/instruments/refresh")
def instruments_refresh(user: dict = Depends(auth.require_user)):
    try:
        inst = refresh_instruments(force=True)
    except UpstoxError as e:
        raise HTTPException(502, str(e))
    return {"instruments": int(len(inst))}


@app.get("/healthz")
def healthz():
    return {"ok": True}
