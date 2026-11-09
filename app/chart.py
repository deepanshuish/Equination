"""Chart reading on daily and weekly candles: trend, volume and classic patterns.

For each stock this produces:

* trend    - up / down / sideways on the daily and on the weekly chart, scored
             from moving averages, their slope, and higher highs / higher lows.
* volume   - accumulation (more volume on up days) or distribution, the recent
             volume trend, and today's volume versus average.
* patterns - cup & handle, bull flag and bear flag, each with a status
             (forming / breakout / breakdown), a pivot (the breakout price), a
             stop under the pattern, a measured-move target, and whether volume
             confirms it (it should dry up inside the pattern and surge on the
             breakout).
* outlook  - bullish / neutral / bearish, a weighted blend of the above.

The pattern rules follow the usual definitions (O'Neil for cup & handle,
Bulkowski for flags) with tolerances so real charts qualify. They describe the
chart; they do not guarantee the move.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class TF:
    name: str
    pole_len: tuple[int, int]      # bars for the flag pole
    flag_len: tuple[int, int]      # bars inside the flag
    pole_move: float               # minimum pole move
    cup_len: tuple[int, int]       # bars from left rim to right rim
    cup_depth: tuple[float, float]
    handle_len: tuple[int, int]
    handle_max_depth: float
    vol_avg: int                   # bars in the average-volume baseline
    breakout_vol: float            # breakout volume must be this multiple of average
    hold_days: int                 # trading days to give a measured move
    trend: tuple[int, int, int, int]  # fast MA, slow MA, slope lookback, swing window


DAILY = TF("daily", (3, 15), (3, 15), 0.10, (35, 250), (0.12, 0.35), (5, 25), 0.15, 50, 1.4, 15, (20, 50, 20, 20))
WEEKLY = TF("weekly", (2, 8), (2, 8), 0.15, (7, 65), (0.12, 0.40), (1, 5), 0.20, 20, 1.3, 40, (10, 30, 6, 10))

LABEL = {"cup_handle": "Cup & handle", "bull_flag": "Bull flag", "bear_flag": "Bear flag"}


def _tick(x: float) -> float:
    return round(round(float(x) * 20) / 20, 2)


def to_weekly(d: pd.DataFrame) -> pd.DataFrame:
    """Weekly candles (weeks ending Friday). An unfinished week's volume is scaled up to a
    full week so breakout volume can be judged mid-week."""
    w = d.resample("W-FRI").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    w = w.dropna(subset=["close"])
    if len(w) and len(d):
        days = int(((d.index > w.index[-1] - pd.Timedelta(days=7)) & (d.index <= w.index[-1])).sum())
        if 0 < days < 5:
            w.iloc[-1, w.columns.get_loc("volume")] = w["volume"].iloc[-1] * 5 / days
    return w


# ------------------------------------------------------------------- trend
def trend(d: pd.DataFrame, tf: TF) -> dict:
    fast, slow, slope_n, swing = tf.trend
    c, h, l = d["close"], d["high"], d["low"]
    if len(c) < slow + slope_n or len(c) < 2 * swing:
        return {"dir": "unknown", "score": 0, "note": "not enough history"}
    sf, ss = c.rolling(fast).mean(), c.rolling(slow).mean()
    last = float(c.iloc[-1])
    slope = float(ss.iloc[-1] / ss.iloc[-1 - slope_n] - 1)
    hh = float(h.iloc[-swing:].max()) > float(h.iloc[-2 * swing:-swing].max())
    hl = float(l.iloc[-swing:].min()) > float(l.iloc[-2 * swing:-swing].min())
    unit = "day" if tf.name == "daily" else "week"
    pts = 0
    notes = []
    pts += 1 if last > sf.iloc[-1] else -1
    pts += 1 if last > ss.iloc[-1] else -1
    notes.append(f"{'above' if last > ss.iloc[-1] else 'below'} its {slow}-{unit} average")
    pts += 1 if sf.iloc[-1] > ss.iloc[-1] else -1
    if slope > 0.01:
        pts += 1
        notes.append(f"{slow}-{unit} average rising")
    elif slope < -0.01:
        pts -= 1
        notes.append(f"{slow}-{unit} average falling")
    pts += (1 if hh else -1) + (1 if hl else -1)
    notes.append({(True, True): "higher highs and higher lows", (False, False): "lower highs and lower lows"}
                 .get((hh, hl), "mixed swings"))
    score = round(pts / 6 * 100)
    direction = "up" if pts >= 3 else "down" if pts <= -3 else "sideways"
    return {"dir": direction, "score": score, "note": ", ".join(notes)}


# ------------------------------------------------------------------ volume
def volume(d: pd.DataFrame, w: pd.DataFrame) -> dict:
    c, v = d["close"].to_numpy(float), d["volume"].to_numpy(float)
    if len(c) < 61:
        return {"state": "unknown", "ud_ratio": None, "trend_pct": None, "today_x": None, "weekly_trend_pct": None, "note": ""}
    chg = np.diff(c[-51:])
    vv = v[-50:]
    up, down = vv[chg > 0].sum(), vv[chg < 0].sum()
    ud = float(up / down) if down > 0 else 3.0
    base = v[-61:-11].mean()
    trend_pct = float(v[-10:].mean() / base - 1) * 100 if base > 0 else None
    avg50 = v[-51:-1].mean()
    today_x = float(v[-1] / avg50) if avg50 > 0 else None
    wv = w["volume"].to_numpy(float)
    wtrend = float(wv[-4:].mean() / wv[-16:-4].mean() - 1) * 100 if len(wv) >= 16 and wv[-16:-4].mean() > 0 else None
    state = "accumulation" if ud >= 1.3 else "distribution" if ud <= 0.77 else "neutral"
    note = {"accumulation": "more volume on up days than down days (buyers in control)",
            "distribution": "more volume on down days than up days (sellers in control)",
            "neutral": "volume balanced between up and down days"}[state]
    return {"state": state, "ud_ratio": round(ud, 2), "trend_pct": None if trend_pct is None else round(trend_pct),
            "today_x": None if today_x is None else round(today_x, 2),
            "weekly_trend_pct": None if wtrend is None else round(wtrend), "note": note}


# ---------------------------------------------------------------- patterns
def _result(kind: str, tf: TF, bias: str, status: str, pivot: float, stop: float, target: float,
            start: int, end: int, n: int, vol_dry: bool, vol_x: float | None, idx, extra: str) -> dict:
    confirmed = status in ("breakout", "breakdown") and vol_x is not None and vol_x >= tf.breakout_vol
    return {
        "kind": kind, "label": LABEL[kind], "tf": tf.name, "bias": bias, "status": status,
        "confirmed": bool(confirmed), "pivot": _tick(pivot), "stop": _tick(stop), "target": _tick(target),
        "start_ago": int(n - 1 - start), "end_ago": int(n - 1 - end),
        "start_date": str(idx[start].date()), "volume_dry_up": bool(vol_dry),
        "breakout_volume_x": None if vol_x is None else round(float(vol_x), 2), "hold_days": tf.hold_days,
        "note": extra,
    }


def bull_flag(d: pd.DataFrame, tf: TF) -> dict | None:
    """Sharp rise (the pole), then a short, tight pause that gives back at most half of it
    on lighter volume. Breakout = close above the flag's high."""
    H, L, C, V = (d[k].to_numpy(float) for k in ("high", "low", "close", "volume"))
    n = len(C)
    if n < tf.vol_avg + tf.pole_len[1] + tf.flag_len[1] + 2:
        return None
    avgv = V[-tf.vol_avg - 1:-1].mean()
    for t in range(n - 1 - tf.flag_len[0], n - 2 - tf.flag_len[1] - 1, -1):  # pole top
        top = H[t]
        breakout = C[-1] > H[t:n - 1].max()
        end = n - 2 if breakout else n - 1
        flen = end - t
        if not tf.flag_len[0] <= flen <= tf.flag_len[1] or H[t + 1:end + 1].max() > top:
            continue
        a, b = t - tf.pole_len[1], t - tf.pole_len[0]
        if a < 0:
            continue
        lo_i = a + int(np.argmin(L[a:b + 1]))
        base = L[lo_i]
        if H[lo_i:t].max() > top or top / base - 1 < tf.pole_move:
            continue
        pre = V[max(0, lo_i - tf.vol_avg):lo_i]
        if len(pre) < 5 or V[lo_i + 1:t + 1].mean() < 1.2 * pre.mean():  # a real pole comes on heavy volume
            continue
        pole = top - base
        flow = L[t + 1:end + 1].min()
        if flow < top - 0.5 * pole or C[end] < top - 0.5 * pole:
            continue
        drift = np.polyfit(np.arange(flen), C[t + 1:end + 1], 1)[0] / top if flen >= 2 else 0.0
        if drift > 0.004:  # a flag drifts sideways or down, it does not keep climbing
            continue
        dry = V[t + 1:end + 1].mean() < 0.85 * V[lo_i:t + 1].mean()
        pivot = H[t:end + 1].max()
        status = "breakout" if breakout else "forming"
        retr = (top - flow) / pole * 100
        extra = f"{(top / base - 1) * 100:.0f}% pole in {t - lo_i} {'days' if tf.name == 'daily' else 'weeks'}, flag gave back {retr:.0f}%"
        return _result("bull_flag", tf, "bullish", status, pivot, flow * 0.995, pivot + pole, lo_i, end, n, dry,
                       V[-1] / avgv if breakout and avgv > 0 else None, d.index, extra)
    return None


def bear_flag(d: pd.DataFrame, tf: TF) -> dict | None:
    """Mirror of the bull flag: sharp fall, weak bounce, breakdown below the flag's low."""
    H, L, C, V = (d[k].to_numpy(float) for k in ("high", "low", "close", "volume"))
    n = len(C)
    if n < tf.vol_avg + tf.pole_len[1] + tf.flag_len[1] + 2:
        return None
    avgv = V[-tf.vol_avg - 1:-1].mean()
    for t in range(n - 1 - tf.flag_len[0], n - 2 - tf.flag_len[1] - 1, -1):  # pole bottom
        bottom = L[t]
        breakdown = C[-1] < L[t:n - 1].min()
        end = n - 2 if breakdown else n - 1
        flen = end - t
        if not tf.flag_len[0] <= flen <= tf.flag_len[1] or L[t + 1:end + 1].min() < bottom:
            continue
        a, b = t - tf.pole_len[1], t - tf.pole_len[0]
        if a < 0:
            continue
        hi_i = a + int(np.argmax(H[a:b + 1]))
        peak = H[hi_i]
        if L[hi_i:t].min() < bottom or 1 - bottom / peak < tf.pole_move:
            continue
        pre = V[max(0, hi_i - tf.vol_avg):hi_i]
        if len(pre) < 5 or V[hi_i + 1:t + 1].mean() < 1.2 * pre.mean():
            continue
        pole = peak - bottom
        fhigh = H[t + 1:end + 1].max()
        if fhigh > bottom + 0.5 * pole or C[end] > bottom + 0.5 * pole:
            continue
        drift = np.polyfit(np.arange(flen), C[t + 1:end + 1], 1)[0] / bottom if flen >= 2 else 0.0
        if drift < -0.004:
            continue
        dry = V[t + 1:end + 1].mean() < 0.85 * V[hi_i:t + 1].mean()
        pivot = L[t:end + 1].min()
        status = "breakdown" if breakdown else "forming"
        extra = f"{(1 - bottom / peak) * 100:.0f}% drop in {t - hi_i} {'days' if tf.name == 'daily' else 'weeks'}, bounce recovered {(fhigh - bottom) / pole * 100:.0f}%"
        return _result("bear_flag", tf, "bearish", status, pivot, fhigh * 1.005, pivot - pole, hi_i, end, n, dry,
                       V[-1] / avgv if breakdown and avgv > 0 else None, d.index, extra)
    return None


def cup_handle(d: pd.DataFrame, tf: TF) -> dict | None:
    """After an advance, a rounded U-shaped base 12-35% deep (40% weekly) whose right side
    climbs back to near the old high, then a short, shallow pullback in the upper half of
    the cup (the handle) on drying volume. Breakout = close above the handle's high."""
    H, L, C, V = (d[k].to_numpy(float) for k in ("high", "low", "close", "volume"))
    n = len(C)
    if n < tf.cup_len[0] + tf.handle_len[1] + 10:
        return None
    avgv = V[-tf.vol_avg - 1:-1].mean()
    for r in range(n - 1 - tf.handle_len[0], n - 2 - tf.handle_len[1] - 1, -1):  # right rim
        rh = H[r]
        breakout = C[-1] > H[r:n - 1].max()
        end = n - 2 if breakout else n - 1
        hlen = end - r
        if not tf.handle_len[0] <= hlen <= tf.handle_len[1] or H[r + 1:end + 1].max() > rh:
            continue
        lo, hi = max(0, r - tf.cup_len[1]), r - tf.cup_len[0]
        if hi <= lo:
            continue
        seg = H[lo:r]
        revmax = np.maximum.accumulate(seg[::-1])[::-1]  # revmax[k] = max(H[lo+k : r])
        cands = [lo + k for k in np.where(seg >= revmax)[0] if lo + k <= hi]
        for left in sorted(cands, reverse=True):  # nearest left rim first
            lh = H[left]
            if not 0.9 * lh <= rh <= 1.05 * lh:
                continue
            bot = left + int(np.argmin(L[left:r + 1]))
            bottom = L[bot]
            depth = 1 - bottom / lh
            span = r - left
            if not tf.cup_depth[0] <= depth <= tf.cup_depth[1]:
                continue
            if not 0.2 <= (bot - left) / span <= 0.8 or H[bot:r].max() > rh:
                continue
            rim = min(lh, rh)
            if (C[left:r + 1] <= bottom + (rim - bottom) / 3).mean() < 0.12:  # V-shaped, not a rounded cup
                continue
            pre = max(0, left - max(span, 30))
            if left - pre < 10 or lh < 1.2 * L[pre:left + 1].min():  # needs a prior advance
                continue
            hlow = L[r + 1:end + 1].min()
            if 1 - hlow / rh > tf.handle_max_depth or hlow < bottom + 0.5 * (rim - bottom):
                continue
            dry = V[r + 1:end + 1].mean() < V[left:r + 1].mean()
            pivot = H[r:end + 1].max()
            status = "breakout" if breakout else "forming"
            unit = "days" if tf.name == "daily" else "weeks"
            extra = f"{depth * 100:.0f}% deep cup over {span} {unit}, {(1 - hlow / rh) * 100:.0f}% handle over {hlen} {unit}"
            return _result("cup_handle", tf, "bullish", status, pivot, hlow * 0.995, pivot + (lh - bottom), left, end, n,
                           dry, V[-1] / avgv if breakout and avgv > 0 else None, d.index, extra)
    return None


# -------------------------------------------------------------- analysis
def analyze(d: pd.DataFrame) -> dict:
    """`d`: daily OHLCV indexed by date, oldest first, no gaps in close."""
    d = d.dropna(subset=["close"])
    w = to_weekly(d)
    td, tw = trend(d, DAILY), trend(w, WEEKLY)
    vol = volume(d, w)
    pats = []
    for frame, tf in ((d, DAILY), (w, WEEKLY)):
        for fn in (cup_handle, bull_flag, bear_flag):
            try:
                p = fn(frame, tf)
            except (ValueError, IndexError, np.linalg.LinAlgError):
                p = None
            if p:
                pats.append(p)

    score = 0.4 * tw["score"] + 0.3 * td["score"]
    score += {"accumulation": 15, "distribution": -15}.get(vol["state"], 0)
    for p in pats:
        w_ = 20 if p["status"] in ("breakout", "breakdown") else 10
        score += w_ if p["bias"] == "bullish" else -w_
    score = int(max(-100, min(100, round(score))))
    outlook = "bullish" if score >= 30 else "bearish" if score <= -30 else "neutral"
    return {"daily": td, "weekly": tw, "volume": vol, "patterns": pats, "outlook": outlook, "outlook_score": score,
            "spark": _spark(d, w)}


def _spark(d: pd.DataFrame, w: pd.DataFrame) -> dict:
    """Recent closes and volumes for the mini charts in the UI."""
    dd, ww = d.iloc[-130:], w.iloc[-80:]
    r = lambda s: [round(float(x), 2) for x in s]  # noqa: E731
    return {"d": {"c": r(dd["close"]), "v": [int(x) for x in dd["volume"].fillna(0)],
                  "from": str(dd.index[0].date()) if len(dd) else None},
            "w": {"c": r(ww["close"]), "v": [int(x) for x in ww["volume"].fillna(0)],
                  "from": str(ww.index[0].date()) if len(ww) else None}}
