"""Orchestrates a daily scan: refresh instruments + candles, score, persist.

Runs in a background thread; `state` exposes progress to the UI.
"""
from __future__ import annotations

import logging
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import pandas as pd

from . import db
from .config import HISTORY_DAYS, NIFTY_KEY
from .strategy import (StrategyParams, build_picks, compute_features, eligibility_summary, market_regime,
                       score_features)
from .universe import select_universe
from .upstox_client import AuthError, UpstoxError, fetch_daily_candles, fetch_nse_equities

log = logging.getLogger("equination.scanner")


class ScanState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
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
                "running": self.running, "phase": self.phase, "done": self.done, "total": self.total,
                "message": self.message, "errors": self.errors[-10:], "error_count": len(self.errors),
                "scan_id": self.scan_id, "started_at": self.started_at,
            }

    def set(self, **kw) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)


state = ScanState()


def refresh_instruments(force: bool = False) -> pd.DataFrame:
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


def refresh_candles(keys: list[str], token: str, workers: int = 4) -> None:
    """Incrementally updates candles for `keys` (Nifty is always included)."""
    keys = list(dict.fromkeys([NIFTY_KEY, *keys]))
    last = db.last_candle_dates()
    state.set(phase="candles", done=0, total=len(keys), message="Downloading daily candles from Upstox")
    auth_failed: AuthError | None = None
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_refresh_one, k, last, token): k for k in keys}
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


def run_scan(trigger: str = "manual") -> int:
    """Full pipeline. Returns the scan id. Never raises - failures are stored on the scan row."""
    if state.running:
        raise RuntimeError("A scan is already running")
    settings = db.get_settings()
    params = StrategyParams.from_settings(settings)
    scan_id = db.create_scan({**params.to_dict(), "universe": settings["universe"], "trigger": trigger})
    state.set(running=True, phase="starting", done=0, total=0, message="", errors=[], scan_id=scan_id,
              started_at=datetime.now().isoformat(timespec="seconds"))
    try:
        token = settings.get("access_token", "")
        if not token:
            raise AuthError("No Upstox access token. Open Settings and log in first.")

        state.set(phase="instruments", message="Refreshing NSE instrument master")
        instruments = refresh_instruments()
        universe, missing = select_universe(instruments, settings["universe"])
        if universe.empty:
            raise UpstoxError("Instrument universe is empty - could not load the NSE instrument master.")

        refresh_candles(universe["instrument_key"].tolist(), token)

        state.set(phase="scoring", message="Scoring the universe")
        candles = db.load_candles([NIFTY_KEY, *universe["instrument_key"].tolist()])
        nifty = candles[candles["instrument_key"] == NIFTY_KEY].set_index("date")["close"]
        regime = market_regime(nifty)
        stock_candles = candles[candles["instrument_key"] != NIFTY_KEY]
        if stock_candles.empty:
            raise UpstoxError("No candle data was downloaded. Check the errors list and your token.")
        feats = compute_features(stock_candles)
        scored = score_features(feats, params)
        picks = build_picks(scored, universe, params, regime)
        stats = eligibility_summary(scored)
        stats["missing_symbols"] = missing
        stats["fetch_errors"] = len(state.errors)
        stats["data_as_of"] = str(pd.Timestamp(feats["last_date"].max()).date()) if len(feats) else None
        msg = f"{len(picks)} picks from {stats['eligible']} eligible of {stats['universe']} stocks."
        db.finish_scan(scan_id, "done", msg, regime.to_dict(), stats, picks)
        state.set(phase="done", message=msg)
    except AuthError as e:
        db.finish_scan(scan_id, "needs_login", str(e), None, None, [])
        state.set(phase="needs_login", message=str(e))
    except Exception as e:  # noqa: BLE001
        log.error("scan failed: %s\n%s", e, traceback.format_exc())
        db.finish_scan(scan_id, "failed", f"{type(e).__name__}: {e}", None, None, [])
        state.set(phase="failed", message=f"{type(e).__name__}: {e}")
    finally:
        state.set(running=False)
    return scan_id


def start_scan_async(trigger: str = "manual") -> None:
    if state.running:
        raise RuntimeError("A scan is already running")
    threading.Thread(target=run_scan, args=(trigger,), daemon=True, name="equination-scan").start()
