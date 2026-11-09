"""Orchestrates a scan for one user: refresh instruments + candles, score,
gate on fundamentals / promoter data, add news sentiment, persist.

Scans run on a small worker pool so many users cannot stampede the shared
Upstox/Marketaux rate limits; each user has their own progress state.
"""
from __future__ import annotations

import logging
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import pandas as pd

from . import db, fundamentals as fnd, overview, sentiment as snt, swing
from .config import HISTORY_DAYS, NIFTY_KEY
from .strategy import market_regime
from .universe import select_universe
from .upstox_client import AuthError, UpstoxError, fetch_daily_candles, fetch_nse_equities

log = logging.getLogger("equination.scanner")
MAX_CONCURRENT_SCANS = 2
_pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_SCANS, thread_name_prefix="equination-scan")
_instruments_lock = threading.Lock()


class ScanState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.queued = False
        self.phase = "idle"
        self.done = 0
        self.total = 0
        self.message = ""
        self.errors: list[str] = []
        self.scan_id: int | None = None
        self.started_at: str | None = None

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running or self.queued, "queued": self.queued, "phase": self.phase,
                "done": self.done, "total": self.total, "message": self.message, "errors": self.errors[-10:],
                "error_count": len(self.errors), "scan_id": self.scan_id, "started_at": self.started_at,
            }

    def set(self, **kw) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)


_states: dict[int, ScanState] = {}
_states_lock = threading.Lock()


def state_for(user_id: int) -> ScanState:
    with _states_lock:
        if user_id not in _states:
            _states[user_id] = ScanState()
        return _states[user_id]


# -------------------------------------------------------------- data refresh
def refresh_instruments(force: bool = False) -> pd.DataFrame:
    with _instruments_lock:
        inst = db.get_instruments()
        stale = inst.empty or (
            pd.Timestamp(inst["updated_at"].max()) < pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=7)
        )
        if force or stale:
            rows = fetch_nse_equities()
            if rows:
                db.upsert_instruments(rows)
                inst = db.get_instruments()
        return inst


def _refresh_one(key: str, last: dict[str, str], token: str) -> tuple[str, int]:
    today = date.today()
    if key in last:
        start = date.fromisoformat(last[key]) - timedelta(days=3)  # re-fetch a few bars in case of revisions
    else:
        start = today - timedelta(days=HISTORY_DAYS)
    if start > today:
        return key, 0
    df = fetch_daily_candles(key, start, today, token)
    return key, db.upsert_candles(key, df)


def refresh_candles(keys: list[str], token: str, state: ScanState, workers: int = 4) -> None:
    """Incrementally updates candles for `keys` (Nifty is always included).

    Symbols already updated today by another user's scan are skipped, so the
    shared cache means the Nth user's scan is nearly free.
    """
    keys = list(dict.fromkeys([NIFTY_KEY, *keys]))
    last = db.last_candle_dates()
    today = str(date.today())
    todo = [k for k in keys if last.get(k) != today]
    state.set(phase="candles", done=len(keys) - len(todo), total=len(keys), message="Downloading daily candles from Upstox")
    auth_failed: AuthError | None = None
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_refresh_one, k, last, token): k for k in todo}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                fut.result()
            except AuthError as e:
                auth_failed = e
                for f in futures:
                    f.cancel()
                break
            except (UpstoxError, Exception) as e:  # noqa: BLE001 - keep scanning other symbols
                with state.lock:
                    state.errors.append(f"{key}: {e}")
            with state.lock:
                state.done += 1
    if auth_failed:
        raise auth_failed


def gate_fundamentals(keys: list[str], instruments: pd.DataFrame, token: str,
                      qp: fnd.QualityParams, state: ScanState) -> dict[str, dict]:
    meta = instruments.set_index("instrument_key")
    state.set(phase="fundamentals", done=0, total=len(keys), message="Checking fundamentals and promoter holdings")
    out: dict[str, dict] = {}
    for key in keys:
        isin = meta["isin"].get(key) if key in meta.index else None
        name = str(meta["name"].get(key, "") or "") if key in meta.index else ""
        f = None
        if isin:
            try:
                f = fnd.get(isin, token, name) if token else fnd.get_cached(isin, max_age_days=None)
            except AuthError:
                raise
            except Exception as e:  # noqa: BLE001
                with state.lock:
                    state.errors.append(f"{key} fundamentals: {e}")
        out[key] = {"assessment": fnd.assess(f, qp), "fundamentals": f.to_dict() if f else None}
        with state.lock:
            state.done += 1
    return out


def _fund_summary(entry: dict | None) -> dict:
    if not entry:
        return {}
    f = entry.get("fundamentals") or {}
    r = f.get("ratios") or {}
    a = entry["assessment"]
    return {
        "quality_score": a["score"], "quality_flags": a["flags"], "quality_hard": a["hard"],
        "pe": r.get("pe"), "roe": r.get("roe"), "roce": r.get("roce"), "debt_equity": r.get("debt_equity"),
        "net_margin": r.get("net_margin"), "eps_growth": r.get("eps_growth"), "revenue_growth": r.get("revenue_growth"),
        "promoter_pct": f.get("promoter_pct"), "promoter_change_pp": f.get("promoter_change_pp"),
        "pledge_pct": f.get("pledge_pct"), "fii_pct": f.get("fii_pct"), "dii_pct": f.get("dii_pct"),
    }


def _hold_until(as_of: str, hold_days: int) -> tuple[str, str]:
    entry = pd.Timestamp(as_of) + pd.offsets.BDay(1)
    exit_ = entry + pd.offsets.BDay(max(hold_days - 1, 0))
    return str(entry.date()), str(exit_.date())


def _excluded_set(settings: dict[str, str]) -> set[str]:
    return {s.strip().upper() for s in (settings.get("exclude_symbols") or "").split(",") if s.strip()}


# ---------------------------------------------------------------- pipeline
def run_scan(user_id: int, trigger: str = "manual") -> int:
    """Full pipeline for one user. Returns the scan id. Never raises.

    If Upstox rejects the token (they expire at 03:30 IST daily), the stocks
    are still rated from the prices already downloaded, and the scan says so.
    """
    state = state_for(user_id)
    settings = db.get_settings(user_id)
    params = swing.SwingParams.from_settings(settings)
    scan_id = db.create_scan(user_id, {**params.to_dict(), "mode": "swing", "universe": settings["universe"], "trigger": trigger})
    state.set(running=True, queued=False, phase="starting", done=0, total=0, message="", errors=[], scan_id=scan_id,
              started_at=datetime.now().isoformat(timespec="seconds"))
    try:
        token = settings.get("access_token", "")
        if not token:
            raise AuthError("No Upstox access token. Open Settings and connect Upstox first.")

        state.set(phase="instruments", message="Refreshing NSE instrument master")
        instruments = refresh_instruments()
        universe, missing = _universe(instruments, settings)
        if universe.empty:
            raise UpstoxError("Instrument universe is empty - could not load the NSE instrument master.")
        refresh_candles(universe["instrument_key"].tolist(), token, state)
        msg = _rate_and_save(user_id, scan_id, settings, state, token, universe, missing)
        state.set(phase="done", message=msg)
    except AuthError as e:
        if has_cached_data(settings):
            try:
                universe, missing = _universe(db.get_instruments(), settings)
                msg = _rate_and_save(user_id, scan_id, settings, state, None, universe, missing,
                                     stale="Upstox not connected")
                state.set(phase="needs_login", message=f"Upstox not connected ({e}) - {msg}")
            except Exception as e2:  # noqa: BLE001
                log.error("cached rating failed for user %s: %s\n%s", user_id, e2, traceback.format_exc())
                db.finish_scan(scan_id, "needs_login", str(e), None, None, [])
                state.set(phase="needs_login", message=str(e))
        else:
            db.finish_scan(scan_id, "needs_login", str(e), None, None, [])
            state.set(phase="needs_login", message=str(e))
    except Exception as e:  # noqa: BLE001
        log.error("scan failed for user %s: %s\n%s", user_id, e, traceback.format_exc())
        db.finish_scan(scan_id, "failed", f"{type(e).__name__}: {e}", None, None, [])
        state.set(phase="failed", message=f"{type(e).__name__}: {e}")
    finally:
        state.set(running=False, queued=False)
    return scan_id


def _universe(instruments: pd.DataFrame, settings: dict[str, str]):
    universe, missing = select_universe(instruments, settings["universe"])
    excluded = _excluded_set(settings)
    if not universe.empty:
        universe = universe[~universe["symbol"].str.upper().isin(excluded)]
    return universe, missing


def has_cached_data(settings: dict[str, str]) -> bool:
    """True when instruments and candles from an earlier scan are stored locally."""
    inst = db.get_instruments()
    if inst.empty:
        return False
    universe, _ = _universe(inst, settings)
    last = db.last_candle_dates()
    return NIFTY_KEY in last and any(k in last for k in universe["instrument_key"])


def _rate_and_save(user_id: int, scan_id: int, settings: dict[str, str], state: ScanState, token: str | None,
                   universe: pd.DataFrame, missing: list[str], stale: str = "") -> str:
    """Rates every stock from the stored candles and saves the scan. With token=None only
    cached fundamentals and news are used (nothing is fetched)."""
    qp = fnd.QualityParams(require_fundamentals=settings.get("require_fundamentals", "0") == "1")
    params = swing.SwingParams.from_settings(settings)
    names = universe.set_index("instrument_key")
    state.set(phase="scoring", message="Rating every stock")
    candles = db.load_candles([NIFTY_KEY, *universe["instrument_key"].tolist()])
    nifty = candles[candles["instrument_key"] == NIFTY_KEY].set_index("date")["close"]
    regime = market_regime(nifty)
    stock_candles = candles[candles["instrument_key"] != NIFTY_KEY]
    if stock_candles.empty:
        raise UpstoxError("No candle data was downloaded. Check the errors list and your token.")

    rows, stats = _scan_overview(stock_candles, names, token, params, qp, regime, state)
    stats.update(add_sentiment(rows, settings if token else {**settings, "marketaux_key": ""}, state))
    if not token:  # news came from the cache only; the key itself is fine
        stats.update(sentiment_enabled=bool(settings.get("marketaux_key")), sentiment_method="saved news only")
    rows = overview.finalize(rows)
    counts = {a: sum(1 for r in rows if r["action"] == a) for a in ("buy", "wait", "avoid")}
    stats.update(counts)
    stats["mode"] = "swing"
    stats["quant_picks"] = counts["buy"]
    stats["missing_symbols"] = missing
    stats["fetch_errors"] = len(state.errors)
    stats["data_as_of"] = str(pd.Timestamp(stock_candles["date"].max()).date())
    stats["stale"] = stale
    msg = (f"{len(rows)} stocks rated: {counts['buy']} buy, {counts['wait']} wait, {counts['avoid']} avoid; "
           f"{stats['sentiment_scored']} with news sentiment.")
    if stale:
        msg = f"Rated from saved prices up to {stats['data_as_of']} ({stale}). " + msg
    db.finish_scan(scan_id, "done", msg, regime.to_dict(), stats, rows)
    return msg


def rate_from_cache(user_id: int, reason: str) -> int | None:
    """Re-rates every stock from saved prices without contacting Upstox or Marketaux.
    Used once for scans made before every stock was rated. Returns the scan id, or None."""
    settings = db.get_settings(user_id)
    if not has_cached_data(settings):
        return None
    params = swing.SwingParams.from_settings(settings)
    scan_id = db.create_scan(user_id, {**params.to_dict(), "mode": "swing", "universe": settings["universe"], "trigger": "cached"})
    universe, missing = _universe(db.get_instruments(), settings)
    try:
        _rate_and_save(user_id, scan_id, settings, ScanState(), None, universe, missing, stale=reason)
    except Exception as e:  # noqa: BLE001
        log.error("cached rating failed for user %s: %s\n%s", user_id, e, traceback.format_exc())
        db.finish_scan(scan_id, "failed", f"{type(e).__name__}: {e}", None, None, [])
        return None
    return scan_id


def _scan_overview(stock_candles, names, token, p: swing.SwingParams, qp, regime, state):
    """Rates every stock, then runs the fundamentals/promoter checks on the buy and wait candidates."""
    fr = swing.SwingFrames(stock_candles, p)
    as_of = str(fr.c.index[-1].date())
    entry_date, exit_date = _hold_until(as_of, p.hold_days)
    rows = overview.build(fr, names, p, regime.allocation > 0, entry_date, exit_date)

    candidates = [r["instrument_key"] for r in rows if r["action"] in ("buy", "wait")]
    funds = gate_fundamentals(candidates, names.reset_index(), token, qp, state)
    gated_out = 0
    for r in rows:
        f = funds.get(r["instrument_key"])
        if f:
            r.update(_fund_summary(f))
            hard = f["assessment"]["hard"]
            if hard:
                gated_out += 1
                r["action"] = "avoid"
                r["reason"] = "Fails the quality check: " + "; ".join(hard)
                r["entry_date"] = r["hold_until"] = None
                r["entry_type"] = "hold"
        if r["action"] in ("buy", "wait") and r.get("entry"):
            qty, value, risk_amt = swing.size_position(r["entry"], r["stop_loss"], p, max(regime.allocation, 0.5))
            r.update(quantity=qty, position_value=value, risk_amount=risk_amt)
        else:
            r.update(quantity=0, position_value=0, risk_amount=0)
        if r["action"] == "buy":
            r["exit_rules"] = (f"Sell if price hits the stop (₹{r['stop_loss']:,.2f}) or the target (₹{r['target']:,.2f}); "
                               f"otherwise sell at the close on {r['hold_until']}.")
        elif r["action"] == "wait":
            r["exit_rules"] = (f"After buying, place the stop at ₹{r['stop_loss']:,.2f}, sell at ₹{r['target']:,.2f}, "
                               f"or at the close {p.hold_days} trading days after you bought.")
        else:
            r["exit_rules"] = "Not a buy now. If you already hold it, the stop-loss is where to exit."
    stats = {"universe": len(rows), "verified": len(candidates), "gated_out": gated_out,
             "gate_reasons": pd.Series([h for k in candidates for h in funds[k]["assessment"]["hard"]]).value_counts().head(8).to_dict()
             if gated_out else {}}
    return rows, stats


def add_sentiment(rows: list[dict], settings: dict[str, str], state: ScanState | None = None) -> dict:
    """Adds news sentiment to every row (buy and wait stocks are fetched first), and moves
    buy/wait stocks with a cluster of strongly negative news to avoid."""
    state = state or ScanState()
    key = settings.get("marketaux_key", "")
    days = int(float(settings.get("sentiment_days", 7) or 7))
    budget = int(float(settings.get("sweep_budget", 40) or 40))
    order = sorted(rows, key=lambda r: (overview.ACTION_ORDER[r["action"]], -(r.get("score") or 0)))
    pairs = [(r["symbol"], r.get("name") or "") for r in order]
    state.set(phase="sentiment", done=0, total=len(pairs),
              message="Reading news sentiment from Marketaux" if key else "Skipping fresh sentiment (no Marketaux key)")
    err = ""
    try:
        priority = {r["symbol"] for r in rows if r["action"] in ("buy", "wait")}
        sents, meta = snt.collect(pairs, key, days, budget, progress=lambda d, t: state.set(done=d, total=t),
                                  priority=priority)
    except snt.SentimentAuthError as e:
        err = str(e)
        with state.lock:
            state.errors.append(f"marketaux: {e}")
        sents, meta = snt.collect(pairs, "", days, 0)
    err = err or meta.get("error", "")
    scored = vetoed = 0
    for r in rows:
        s = sents.get(r["symbol"])
        if s and s.n_scored:
            scored += 1
        r["sentiment_score"] = s.score if s else None
        r["sentiment_n"] = s.n_scored if s else 0
        r["sentiment_articles"] = s.n_articles if s else 0
        r["sentiment_verdict"] = s.verdict if s else "not_checked"
        r["sentiment_pos"] = s.positive if s else 0
        r["sentiment_neg"] = s.negative if s else 0
        r["sentiment_as_of"] = s.fetched_at if s else None
        r["headlines"] = s.headlines if s else []
        if (r["action"] in ("buy", "wait") and s is not None and s.score is not None
                and s.n_scored >= 2 and s.score <= snt.STRONG_NEGATIVE):
            vetoed += 1
            r["action"] = "avoid"
            r["reason"] = f"Cluster of negative news (sentiment {s.score:+.2f} over {s.n_scored} articles)"
            r["entry_date"] = r["hold_until"] = None
            r["entry_type"] = "hold"
            r["quantity"] = 0
            r["exit_rules"] = "Not a buy now. If you already hold it, the stop-loss is where to exit."
    return {"sentiment_scored": scored, "sentiment_excluded": vetoed, "sentiment_enabled": bool(key),
            "sentiment_error": err, "sentiment_method": meta.get("method"),
            "sentiment_requests": meta.get("requests_used", 0)}


# ------------------------------------------------------------------- sweep
def run_sweep(user_id: int, settings: dict[str, str] | None = None) -> dict:
    settings = settings or db.get_settings(user_id)
    state = state_for(user_id)
    key = settings.get("marketaux_key", "")
    if not key:
        raise snt.SentimentAuthError("No Marketaux API key configured.")
    instruments = db.get_instruments()
    universe, _ = select_universe(instruments, settings["universe"])
    excluded = _excluded_set(settings)
    universe = universe[~universe["symbol"].str.upper().isin(excluded)]
    if universe.empty:
        raise RuntimeError("Instrument universe is empty - run a scan first so the NSE list is cached.")
    pairs = [(r.symbol, str(r.name or "")) for r in universe.itertuples(index=False)]
    state.set(phase="sweep", done=0, total=len(pairs), message="Sweeping universe sentiment via Marketaux")
    res = snt.sweep(pairs, key, days=int(float(settings.get("sentiment_days", 7) or 7)),
                    budget=int(float(settings.get("sweep_budget", 40) or 40)),
                    progress=lambda d, t: state.set(done=d, total=t))
    db.save_sweep(user_id, res)
    return res


# -------------------------------------------------------------- scheduling
def _submit(user_id: int, fn, *args) -> None:
    state = state_for(user_id)
    if state.running or state.queued:
        raise RuntimeError("A scan is already running for this account")
    state.set(queued=True, phase="queued", message="Waiting for a worker", done=0, total=0, errors=[])
    _pool.submit(fn, user_id, *args)


def start_scan_async(user_id: int, trigger: str = "manual") -> None:
    _submit(user_id, run_scan, trigger)


def start_sweep_async(user_id: int) -> None:
    def _go(uid: int) -> None:
        state = state_for(uid)
        state.set(running=True, queued=False, phase="sweep", done=0, total=0, message="", errors=[], scan_id=None,
                  started_at=datetime.now().isoformat(timespec="seconds"))
        try:
            res = run_sweep(uid)
            state.set(phase="done", message=f"sweep done: {res['scored']} of {res['covered']} symbols scored "
                                            f"({res['method']}, {res['requests_used']} requests)")
        except Exception as e:  # noqa: BLE001
            log.error("sweep failed for user %s: %s\n%s", uid, e, traceback.format_exc())
            state.set(phase="failed", message=f"sweep failed: {e}")
        finally:
            state.set(running=False, queued=False)

    _submit(user_id, _go)
