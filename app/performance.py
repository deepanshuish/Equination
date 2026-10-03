"""Grades every past pick against what actually happened afterwards.

For each completed scan, each pick is simulated the way the dashboard tells
you to trade it: buy at the next session's open, exit at the stop (or the open
if it gapped through), at the target, or at the close of the last holding day.
Open positions are marked to the latest close. This is a forward test of the
real picks, not a backtest, so it is the honest measure of the tool.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from . import db
from .config import NIFTY_KEY

COST_PCT = 0.15  # round trip, delivery


@dataclass
class Outcome:
    scan_id: int
    scan_date: str
    mode: str
    symbol: str
    instrument_key: str
    list: str  # quant | cumulative | both
    quant_rank: int
    cum_rank: int
    signal_close: float
    entry_date: str | None
    entry: float | None
    stop: float | None
    target: float | None
    exit_date: str | None
    exit: float | None
    exit_reason: str  # stop | target | time | open | no_data
    days_held: int
    ret_pct: float | None
    nifty_ret_pct: float | None
    sentiment_verdict: str
    status: str  # closed | open | pending

    def to_dict(self) -> dict:
        return asdict(self)


def _simulate(bars: pd.DataFrame, as_of: pd.Timestamp, stop: float | None, target: float | None,
              hold_days: int, mode: str) -> dict:
    """bars: OHLC indexed by date for one symbol. Returns the exit details."""
    fwd = bars[bars.index > as_of]
    if fwd.empty:
        return {"status": "pending", "exit_reason": "no_data"}
    entry_bar = fwd.iloc[0]
    entry = float(entry_bar["open"]) if pd.notna(entry_bar["open"]) else float(entry_bar["close"])
    entry_date = fwd.index[0]
    horizon = fwd.iloc[:hold_days] if mode == "swing" else fwd
    exit_px = exit_date = None
    reason = "open"
    for i, (d, row) in enumerate(horizon.iterrows()):
        lo, hi, op, cl = float(row["low"]), float(row["high"]), float(row["open"]), float(row["close"])
        if stop is not None and i > 0 and op <= stop:
            exit_px, exit_date, reason = op, d, "stop"
            break
        if stop is not None and lo <= stop:
            exit_px, exit_date, reason = stop, d, "stop"
            break
        if target is not None and hi >= target:
            exit_px, exit_date, reason = target, d, "target"
            break
    if exit_px is None:
        if mode == "swing" and len(fwd) >= hold_days:
            last = horizon.iloc[-1]
            exit_px, exit_date, reason = float(last["close"]), horizon.index[-1], "time"
        else:
            last = fwd.iloc[-1]
            exit_px, exit_date, reason = float(last["close"]), fwd.index[-1], "open"
    status = "open" if reason == "open" else "closed"
    days = int((fwd.index <= exit_date).sum())
    ret = (exit_px / entry - 1) * 100 - (COST_PCT if status == "closed" else 0.0)
    return {"status": status, "entry": entry, "entry_date": entry_date, "exit": exit_px, "exit_date": exit_date,
            "exit_reason": reason, "days_held": days, "ret_pct": ret}


def grade_all(limit_scans: int = 200) -> dict:
    scans = [s for s in db.list_scans(limit_scans) if s["status"] == "done"]
    if not scans:
        return {"outcomes": [], "stats": {}, "by_list": {}, "curve": []}
    keys: set[str] = set()
    full: list[dict] = []
    for s in scans:
        detail = db.get_scan(s["id"])
        if not detail:
            continue
        full.append(detail)
        for r in detail["results"]:
            if r.get("rank", 0) or r.get("cum_rank", 0):
                keys.add(r["instrument_key"])
    if not keys:
        return {"outcomes": [], "stats": {}, "by_list": {}, "curve": []}
    candles = db.load_candles([NIFTY_KEY, *sorted(keys)])
    bars = {k: g.set_index("date")[["open", "high", "low", "close"]].sort_index() for k, g in candles.groupby("instrument_key")}
    nifty = bars.get(NIFTY_KEY)

    outcomes: list[Outcome] = []
    for detail in full:
        mode = detail["stats"].get("mode") or detail["params"].get("mode") or "positional"
        hold = int(detail["params"].get("hold_days", 4) or 4)
        as_of_str = detail["stats"].get("data_as_of") or detail["run_at"][:10]
        as_of = pd.Timestamp(as_of_str)
        for r in detail["results"]:
            qr, cr = int(r.get("rank", 0) or 0), int(r.get("cum_rank", 0) or 0)
            if not qr and not cr:
                continue
            b = bars.get(r["instrument_key"])
            stop = r.get("stop_loss")
            target = r.get("target") if mode == "swing" else None
            sim = _simulate(b, as_of, stop, target, hold, mode) if b is not None else {"status": "pending", "exit_reason": "no_data"}
            nret = None
            if nifty is not None and sim.get("entry_date") is not None and sim.get("exit_date") is not None:
                try:
                    n0 = float(nifty.loc[sim["entry_date"], "open"]); n1 = float(nifty.loc[sim["exit_date"], "close"])
                    nret = (n1 / n0 - 1) * 100
                except KeyError:
                    nret = None
            outcomes.append(Outcome(
                scan_id=detail["id"], scan_date=as_of_str, mode=mode, symbol=r["symbol"], instrument_key=r["instrument_key"],
                list="both" if qr and cr else ("quant" if qr else "cumulative"), quant_rank=qr, cum_rank=cr,
                signal_close=float(r.get("entry") or r.get("close") or 0),
                entry_date=str(sim["entry_date"].date()) if sim.get("entry_date") is not None else None,
                entry=round(sim["entry"], 2) if sim.get("entry") is not None else None,
                stop=stop, target=target,
                exit_date=str(sim["exit_date"].date()) if sim.get("exit_date") is not None else None,
                exit=round(sim["exit"], 2) if sim.get("exit") is not None else None,
                exit_reason=sim["exit_reason"], days_held=int(sim.get("days_held", 0)),
                ret_pct=round(sim["ret_pct"], 2) if sim.get("ret_pct") is not None else None,
                nifty_ret_pct=round(nret, 2) if nret is not None else None,
                sentiment_verdict=r.get("sentiment_verdict") or "no_news", status=sim["status"],
            ))
    outcomes.sort(key=lambda o: (o.scan_date, o.quant_rank or 99), reverse=True)
    df = pd.DataFrame([o.to_dict() for o in outcomes])
    return {
        "outcomes": [o.to_dict() for o in outcomes],
        "stats": _stats(df[df["status"] == "closed"]) | {"open": int((df["status"] == "open").sum()),
                                                        "pending": int((df["status"] == "pending").sum())},
        "by_list": {name: _stats(df[(df["status"] == "closed") & df["list"].isin(members)])
                    for name, members in (("quant", ["quant", "both"]), ("cumulative", ["cumulative", "both"]))},
        "by_sentiment": {v: _stats(g) for v, g in df[df["status"] == "closed"].groupby("sentiment_verdict")},
        "curve": _curve(df[df["status"] == "closed"]),
    }


def _stats(df: pd.DataFrame) -> dict:
    if df.empty or df["ret_pct"].dropna().empty:
        return {"trades": 0}
    r = df["ret_pct"].dropna()
    wins = r > 0
    gw, gl = r[wins].sum(), -r[~wins].sum()
    n = df["nifty_ret_pct"].dropna()
    return {
        "trades": int(len(r)), "win_rate_pct": round(float(wins.mean()) * 100, 1),
        "avg_ret_pct": round(float(r.mean()), 2), "median_ret_pct": round(float(r.median()), 2),
        "avg_win_pct": round(float(r[wins].mean()), 2) if wins.any() else 0.0,
        "avg_loss_pct": round(float(r[~wins].mean()), 2) if (~wins).any() else 0.0,
        "profit_factor": round(float(gw / gl), 2) if gl > 0 else None,
        "total_ret_pct": round(float(r.sum()), 2),
        "nifty_avg_pct": round(float(n.mean()), 2) if len(n) else None,
        "exits": df["exit_reason"].value_counts().to_dict(),
    }


def _curve(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    d = df.dropna(subset=["ret_pct", "exit_date"]).sort_values("exit_date")
    eq = (1 + d["ret_pct"] / 100).cumprod()
    return [{"date": x, "equity": round(float(e), 4)} for x, e in zip(d["exit_date"], eq)]
