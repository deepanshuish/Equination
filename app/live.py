"""Live prices for the overview, between scans.

One Upstox full-quote request covers every stock in the latest scan. Each row
gets the latest traded price, the change from the previous close, and where
the price stands against that row's plan: whether a buy-stop or dip limit has
triggered, and whether the stop-loss or target has been touched today.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from .upstox_client import fetch_quotes

IST = timezone(timedelta(hours=5, minutes=30))
CACHE_SECONDS = 20
_cache: dict[int, tuple[float, dict]] = {}
_lock = threading.Lock()


def market_open(now: datetime | None = None) -> bool:
    """NSE regular session, 09:15-15:30 IST on weekdays (exchange holidays are not known here)."""
    now = (now or datetime.now(IST)).astimezone(IST)
    return now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) < (15, 30)


def _quote(q: dict) -> dict | None:
    ltp = q.get("last_price")
    if ltp is None:
        return None
    ohlc = q.get("ohlc") or {}
    chg = q.get("net_change")
    prev = ltp - chg if chg is not None else None
    ts = q.get("last_trade_time") or ""
    try:
        traded = datetime.fromtimestamp(int(ts) / 1000, IST)
    except (TypeError, ValueError):
        traded = None
    if traded is None and q.get("timestamp"):
        try:
            traded = datetime.fromisoformat(str(q["timestamp"])).astimezone(IST)
        except ValueError:
            traded = None
    return {
        "ltp": round(float(ltp), 2), "prev_close": round(prev, 2) if prev else None,
        "change_pct": round((ltp / prev - 1) * 100, 2) if prev else None,
        "open": ohlc.get("open"), "high": ohlc.get("high"), "low": ohlc.get("low"),
        "volume": q.get("volume"),
        "traded_at": traded.isoformat(timespec="seconds") if traded else None,
        "date": traded.date().isoformat() if traded else None,
    }


def status(row: dict, q: dict) -> dict:
    """Where today's price stands against the row's plan: {code, text}.

    Today's high/low are used only when today is the plan's buy day (the day
    after the scan's last candle); on later days only the current price is.
    """
    ltp, hi, lo, op = q["ltp"], q.get("high") or q["ltp"], q.get("low") or q["ltp"], q.get("open")
    stop, target, entry = row.get("stop_loss"), row.get("target"), row.get("entry")
    action, kind = row.get("action"), row.get("entry_type")
    same_day = bool(row.get("entry_date")) and q.get("date") == row.get("entry_date")
    if not same_day:
        hi = lo = ltp
    pct = lambda a, b: (a / b - 1) * 100  # noqa: E731

    if action == "avoid" or entry is None:
        if stop is not None and ltp <= stop:
            return {"code": "below_stop", "text": "below the stop-loss — exit if you hold it"}
        if stop is not None:
            return {"code": "info", "text": f"{pct(ltp, stop):.1f}% above the stop-loss"}
        return {"code": "info", "text": ""}

    entry_day = row.get("entry_date") or ""
    early = bool(entry_day) and bool(q.get("date")) and q["date"] < entry_day  # plan starts on a later session
    late = bool(entry_day) and bool(q.get("date")) and q["date"] > entry_day   # plan was for an earlier session
    bought = None
    if action == "buy":
        if early:
            return {"code": "pending", "text": f"buy at the open on {_day(entry_day)} · now {pct(ltp, entry):+.1f}% vs plan"}
        if late:
            return {"code": "info", "text": f"plan was for {_day(entry_day)} · now {pct(ltp, entry):+.1f}% vs that buy price"
                    + (" · below the stop-loss" if stop is not None and ltp <= stop else "")}
        if same_day and op:
            if stop is not None and op <= stop:
                return {"code": "skip", "text": f"opened at ₹{op:,.2f}, below the stop-loss — skip this trade"}
            bought = op
        else:
            bought = entry
    elif kind == "above":
        if hi >= entry:
            bought = max(entry, op) if same_day and op else entry
        else:
            return {"code": "pending", "text": f"needs +{pct(entry, ltp):.1f}% to trigger the buy-stop"}
    elif kind == "dip":
        if lo <= entry:
            bought = min(entry, op) if same_day and op else entry
        else:
            return {"code": "pending", "text": f"needs −{pct(ltp, entry):.1f}% to reach the limit"}
    else:
        bought = entry

    if stop is not None and lo <= stop:
        return {"code": "stopped", "text": f"stop-loss ₹{stop:,.2f} hit — sell"}
    if target is not None and hi >= target:
        return {"code": "target", "text": f"target ₹{target:,.2f} hit — book profit"}
    how = "bought at the open" if action == "buy" and same_day and op else "triggered"
    away = f" · stop {abs(pct(stop, ltp)):.1f}% below" if stop is not None else ""
    return {"code": "active", "text": f"{how} ₹{bought:,.2f} · now {pct(ltp, bought):+.1f}%{away}"}


def _day(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%a %d %b")


def quotes_for(user_id: int, rows: list[dict], token: str, force: bool = False) -> dict:
    """Live quotes and plan status for every row, cached for a few seconds per user."""
    now = time.time()
    with _lock:
        hit = _cache.get(user_id)
        if hit and not force and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    raw = fetch_quotes([r["instrument_key"] for r in rows if r.get("instrument_key")], token)
    out: dict[str, dict] = {}
    for r in rows:
        q = raw.get(r.get("instrument_key"))
        q = _quote(q) if q else None
        if q:
            out[r["instrument_key"]] = {**q, "status": status(r, q)}
    res = {"fetched_at": datetime.now(IST).isoformat(timespec="seconds"), "market_open": market_open(),
           "count": len(out), "quotes": out}
    with _lock:
        _cache[user_id] = (now, res)
    return res
