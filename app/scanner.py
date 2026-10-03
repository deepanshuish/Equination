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

from . import db, fundamentals as fnd, sentiment as snt, swing
from .config import HISTORY_DAYS, NIFTY_KEY
from .strategy import (StrategyParams, build_picks, compute_features, eligibility_summary, market_regime,
                       score_features)
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
                f = fnd.get(isin, token, name)
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
    """Full pipeline for one user. Returns the scan id. Never raises."""
    state = state_for(user_id)
    settings = db.get_settings(user_id)
    mode = settings.get("mode", "swing")
    qp = fnd.QualityParams(require_fundamentals=settings.get("require_fundamentals", "0") == "1")
    params = swing.SwingParams.from_settings(settings) if mode == "swing" else StrategyParams.from_settings(settings)
    scan_id = db.create_scan(user_id, {**params.to_dict(), "mode": mode, "universe": settings["universe"], "trigger": trigger})
    state.set(running=True, queued=False, phase="starting", done=0, total=0, message="", errors=[], scan_id=scan_id,
              started_at=datetime.now().isoformat(timespec="seconds"))
    try:
        token = settings.get("access_token", "")
        if not token:
            raise AuthError("No Upstox access token. Open Settings and connect Upstox first.")

        state.set(phase="instruments", message="Refreshing NSE instrument master")
        instruments = refresh_instruments()
        universe, missing = select_universe(instruments, settings["universe"])
        if universe.empty:
            raise UpstoxError("Instrument universe is empty - could not load the NSE instrument master.")
        excluded = _excluded_set(settings)
        universe = universe[~universe["symbol"].str.upper().isin(excluded)]
        names = universe.set_index("instrument_key")

        refresh_candles(universe["instrument_key"].tolist(), token, state)

        state.set(phase="scoring", message="Scoring the universe")
        candles = db.load_candles([NIFTY_KEY, *universe["instrument_key"].tolist()])
        nifty = candles[candles["instrument_key"] == NIFTY_KEY].set_index("date")["close"]
        regime = market_regime(nifty)
        stock_candles = candles[candles["instrument_key"] != NIFTY_KEY]
        if stock_candles.empty:
            raise UpstoxError("No candle data was downloaded. Check the errors list and your token.")

        if mode == "swing":
            picks, stats = _scan_swing(stock_candles, names, token, params, qp, regime, state)
        else:
            picks, stats = _scan_positional(stock_candles, universe, token, params, qp, regime, state)

        picks, sstats = add_sentiment_and_rank(picks, settings, params.top_n, state)
        stats.update(sstats)
        stats["mode"] = mode
        stats["missing_symbols"] = missing
        stats["fetch_errors"] = len(state.errors)
        stats["data_as_of"] = str(pd.Timestamp(stock_candles["date"].max()).date())
        msg = (f"{stats['quant_picks']} quant / {stats['cumulative_picks']} cumulative picks ({mode}); "
               f"{stats.get('setups', stats.get('eligible', 0))} candidates, "
               f"{stats.get('gated_out', 0)} removed by fundamentals/promoter checks, "
               f"{stats['sentiment_scored']} with news sentiment.")
        db.finish_scan(scan_id, "done", msg, regime.to_dict(), stats, picks)
        if settings.get("sweep_in_scan", "0") == "1" and settings.get("marketaux_key"):
            try:
                run_sweep(user_id, settings)
            except Exception as e:  # noqa: BLE001 - the scan itself succeeded
                with state.lock:
                    state.errors.append(f"sweep: {e}")
        state.set(phase="done", message=msg)
    except AuthError as e:
        db.finish_scan(scan_id, "needs_login", str(e), None, None, [])
        state.set(phase="needs_login", message=str(e))
    except Exception as e:  # noqa: BLE001
        log.error("scan failed for user %s: %s\n%s", user_id, e, traceback.format_exc())
        db.finish_scan(scan_id, "failed", f"{type(e).__name__}: {e}", None, None, [])
        state.set(phase="failed", message=f"{type(e).__name__}: {e}")
    finally:
        state.set(running=False, queued=False)
    return scan_id


def _scan_swing(stock_candles, names, token, p: swing.SwingParams, qp, regime, state):
    fr = swing.SwingFrames(stock_candles, p)
    cands = swing.latest_candidates(fr, regime.allocation > 0)
    setups = cands[cands["setup"]]
    shortlist = setups.head(p.top_n * 3).index.tolist()
    funds = gate_fundamentals(shortlist, names.reset_index(), token, qp, state)
    picks: list[dict] = []
    gated_out = 0
    entry_date, exit_date = _hold_until(cands["as_of"].iloc[0] if len(cands) else str(date.today()), p.hold_days)
    for key in shortlist:
        a = funds[key]["assessment"]
        if not a["ok"]:
            gated_out += 1
            continue
        r = cands.loc[key]
        plan = fr.plan(key)
        qty, value, risk_amt = swing.size_position(plan["entry"], plan["stop"], p, regime.allocation)
        picks.append({
            "quant_rank": len(picks) + 1, "instrument_key": key,
            "symbol": names["symbol"].get(key, key), "name": names["name"].get(key, ""),
            "close": round(float(r["close"]), 2), "as_of": r["as_of"],
            "score": round(float(r["score"]), 1),
            "patterns": list(r["patterns"]), "pullback_pct": r["pullback_pct"], "rsi2": r["rsi2"],
            "rsi2_prev": r["rsi2_prev"], "ret_6": r["ret_6m"], "ret_1": r["ret_1m"], "atr_pct": r["atr_pct"],
            "vol_ratio": r["vol_ratio"], "sma50": r["sma50"], "sma200": r["sma200"], "turnover_cr": r["turnover_cr"],
            "entry": plan["entry"], "stop_loss": plan["stop"], "target": plan["target"],
            "reward_risk": plan["reward_risk"], "entry_date": entry_date, "hold_until": exit_date,
            "quantity": qty, "position_value": value, "risk_amount": risk_amt,
            **_fund_summary(funds[key]),
            "exit_rules": f"Sell if price hits the stop or the target, otherwise sell at the close on {exit_date}.",
        })
    excl = cands.loc[~cands["setup"], "excluded"].str.split("; ").explode().value_counts().head(8).to_dict()
    stats = {"universe": int(len(cands)), "setups": int(len(setups)), "verified": len(shortlist),
             "gated_out": gated_out, "excluded_reasons": excl,
             "gate_reasons": pd.Series([h for k in shortlist for h in funds[k]["assessment"]["hard"]]).value_counts().head(8).to_dict()
             if gated_out else {}}
    return picks, stats


def _scan_positional(stock_candles, universe, token, p: StrategyParams, qp, regime, state):
    feats = compute_features(stock_candles)
    scored = score_features(feats, p)
    shortlist = scored[scored["eligible"]].head(p.top_n * 3).index.tolist()
    funds = gate_fundamentals(shortlist, universe, token, qp, state)
    bad = [k for k in shortlist if not funds[k]["assessment"]["ok"]]
    scored.loc[bad, "eligible"] = False
    scored.loc[bad, "excluded"] = "fundamentals/promoter gate"
    scored["rank"] = scored["eligible"].cumsum().where(scored["eligible"], 0)
    picks = build_picks(scored, universe, p, regime, limit=len(shortlist))
    for pk in picks:
        pk["quant_rank"] = pk.pop("rank")
        pk.update(_fund_summary(funds.get(pk["instrument_key"])))
    stats = eligibility_summary(scored)
    stats.update({"verified": len(shortlist), "gated_out": len(bad),
                  "gate_reasons": pd.Series([h for k in bad for h in funds[k]["assessment"]["hard"]]).value_counts().head(8).to_dict()
                  if bad else {}})
    return picks, stats


def add_sentiment_and_rank(picks: list[dict], settings: dict[str, str], top_n: int,
                           state: ScanState | None = None) -> tuple[list[dict], dict]:
    """Adds news sentiment to every gate-passing candidate and assigns

    * `rank`      - quant rank (numbers only), 0 when outside the top N
    * `cum_rank`  - cumulative rank blending quant score, sentiment and quality
    """
    state = state or ScanState()
    key = settings.get("marketaux_key", "")
    days = int(float(settings.get("sentiment_days", 7) or 7))
    wq = float(settings.get("w_quant", 55)); ws = float(settings.get("w_sentiment", 30)); wf = float(settings.get("w_quality", 15))
    wsum = (wq + ws + wf) or 100.0
    state.set(phase="sentiment", done=0, total=len(picks),
              message="Reading news sentiment from Marketaux" if key else "Skipping sentiment (no Marketaux key)")
    scored = 0
    auth_err = ""
    n = max(len(picks), 1)
    for pk in picks:
        s = None
        if key and not auth_err:
            try:
                s = snt.get_cached(pk["symbol"]) or snt.fetch(pk["symbol"], pk.get("name") or "", key, days)
            except snt.SentimentAuthError as e:
                auth_err = str(e)
                with state.lock:
                    state.errors.append(f"marketaux: {e}")
            except snt.SentimentError as e:
                with state.lock:
                    state.errors.append(f"{pk['symbol']} sentiment: {e}")
                s = snt.get_cached(pk["symbol"], max_age_hours=72)
        elif not key:
            s = snt.get_cached(pk["symbol"], max_age_hours=72)
        if s and s.n_scored:
            scored += 1
        pk["sentiment_score"] = s.score if s else None
        pk["sentiment_n"] = s.n_scored if s else 0
        pk["sentiment_articles"] = s.n_articles if s else 0
        pk["sentiment_verdict"] = s.verdict if s else "no_news"
        pk["sentiment_pos"] = s.positive if s else 0
        pk["sentiment_neg"] = s.negative if s else 0
        pk["headlines"] = s.headlines if s else []
        quant_pts = 100.0 * (1 - (pk["quant_rank"] - 1) / n)
        sent_pts = snt.sentiment_points(s)
        qual_pts = float(pk.get("quality_score") or 50.0)
        pk["quant_points"] = round(quant_pts, 1)
        pk["sentiment_points"] = sent_pts
        pk["cum_score"] = round((wq * quant_pts + ws * sent_pts + wf * qual_pts) / wsum, 1)
        pk["cum_excluded"] = (s is not None and s.score is not None and s.n_scored >= 2 and s.score <= snt.STRONG_NEGATIVE)
        pk["rank"] = pk["quant_rank"] if pk["quant_rank"] <= top_n else 0
        with state.lock:
            state.done += 1
    order = sorted([p for p in picks if not p["cum_excluded"]], key=lambda p: -p["cum_score"])
    for i, pk in enumerate(order):
        pk["cum_rank"] = i + 1 if i < top_n else 0
    for pk in picks:
        pk.setdefault("cum_rank", 0)
    stats = {
        "quant_picks": sum(1 for p in picks if p["rank"]),
        "cumulative_picks": sum(1 for p in picks if p["cum_rank"]),
        "sentiment_scored": scored,
        "sentiment_excluded": sum(1 for p in picks if p["cum_excluded"]),
        "sentiment_enabled": bool(key),
        "sentiment_error": auth_err,
        "weights": {"quant": wq, "sentiment": ws, "quality": wf},
    }
    return picks, stats


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
