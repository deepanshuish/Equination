"""Overview: a verdict and a trade plan for every stock in the universe.

Each stock gets one of three actions, judged on the latest daily close:

* buy   - the full swing setup fired today (uptrend, 3-12% pullback, oversold,
          bullish reversal candle) and the market is not in a downtrend.
          Buy at the next open.
* wait  - the stock is in a healthy uptrend but the setup is not complete.
          Either it has pulled back and needs a reversal (buy only once price
          trades above today's high), or it is near its high (buy on a dip to
          a limit price).
* avoid - not in an uptrend, too volatile/illiquid/parabolic, falling too
          steeply, failing the fundamentals/promoter checks, or hit by a
          cluster of negative news.

The chart reader (chart.py) adds daily and weekly trend, volume and patterns:
a volume-confirmed cup & handle or bull flag breakout becomes a buy with the
pattern's stop and measured-move target; a pattern still forming becomes a
wait with a buy-stop just above its pivot; a bear flag breakdown is an avoid.

Every row carries an entry, stop-loss and target. For "avoid" rows the levels
are where a holder would exit, not a buy suggestion.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import chart, patterns as pat
from .swing import SwingFrames, SwingParams

PATTERN_LABEL = {"morning_star": "morning star", "bullish_engulfing": "bullish engulfing", "piercing": "piercing line",
                 "hammer": "hammer", "bullish_harami": "bullish harami", "strong_close": "strong close"}
DIP_ENTRY = 0.05  # "wait for a dip" limit price: 5% below the 20-day high
ACTION_ORDER = {"buy": 0, "wait": 1, "avoid": 2}


def tick(x: float) -> float:
    """Rounds to NSE's 5-paise tick."""
    return round(round(x * 20) / 20, 2)


def _f(frame: pd.DataFrame, key: str) -> float:
    v = frame[key].iloc[-1]
    return float(v) if pd.notna(v) else float("nan")


def _avoid_reason(fr: SwingFrames, key: str, p: SwingParams) -> str:
    if fr.bars[key].iloc[-1] < 220:
        return "Not enough price history (needs about a year)"
    close = _f(fr.c, key)
    if close < p.min_price:
        return f"Price below ₹{p.min_price:g}"
    if not (_f(fr.turnover_cr, key) >= p.min_turnover_cr):
        return f"Too illiquid (under ₹{p.min_turnover_cr:g} cr traded a day)"
    if _f(fr.ret_1m, key) > 0.25:
        return "Up more than 25% in a month — too stretched to buy"
    atr_pct = _f(fr.atr_pct, key)
    if not bool(fr.sane[key].iloc[-1]):
        return "Too volatile" if atr_pct > p.max_atr_pct else "Too quiet to swing trade"
    if close < _f(fr.sma200, key):
        return "In a downtrend (below its 200-day average)"
    if _f(fr.sma50, key) <= _f(fr.sma200, key):
        return "Trend is weak (50-day average below 200-day)"
    if close < 0.95 * _f(fr.sma50, key):
        return "Fell well below its 50-day average"
    return "Negative 6-month return"


def build(fr: SwingFrames, names: pd.DataFrame, p: SwingParams, regime_ok: bool,
          entry_date: str, exit_date: str) -> list[dict]:
    """One row per stock with action, reason, entry, when, stop and target."""
    rows: list[dict] = []
    as_of = str(fr.c.index[-1].date())
    for key in names.index:
        sym = names["symbol"].get(key, key)
        base = {"instrument_key": key, "symbol": sym, "name": names["name"].get(key, ""), "as_of": as_of}
        if key not in fr.c.columns or pd.isna(fr.c[key].iloc[-1]):
            rows.append({**base, "action": "avoid", "reason": "No price data for the latest session",
                         "close": None, "entry": None, "stop_loss": None, "target": None, "score": 0.0})
            continue
        close = _f(fr.c, key)
        high = _f(fr.h, key)
        high20 = _f(fr.high20, key)
        pb = _f(fr.pullback, key)
        trend = bool(fr.trend[key].iloc[-1])
        sane = bool(fr.sane[key].iloc[-1])
        dip = bool(fr.rsi_dip[key].iloc[-1])
        setup = bool(fr.setup[key].iloc[-1])
        candles = [PATTERN_LABEL.get(x, x) for x in pat.names_on_bar(fr.patterns, key)]

        if setup and regime_ok:
            action, entry_type, entry = "buy", "open", close
            reason = f"Uptrend, pulled back {pb * 100:.1f}%, then a {', '.join(candles) or 'reversal'} candle today"
        elif setup:
            action, entry_type, entry = "wait", "above", tick(high * 1.001)
            reason = ("Setup fired, but the market is in a downtrend (Nifty below its 200-day average) — "
                      "buy only if it keeps rising above today's high")
        elif trend and sane and pb > p.max_pullback:
            action, entry_type, entry = "avoid", "hold", close
            reason = f"Dropped {pb * 100:.1f}% from its 20-day high — falling too steeply to buy the dip"
        elif trend and sane and pb >= p.min_pullback:
            action, entry_type, entry = "wait", "above", tick(high * 1.001)
            reason = (f"Uptrend, pulled back {pb * 100:.1f}%; no reversal candle yet — buy once it trades above today's high"
                      if dip else
                      f"Uptrend, pulled back {pb * 100:.1f}% but not oversold yet — buy once it trades above today's high")
        elif trend and sane:
            action, entry_type = "wait", "dip"
            entry = tick(min(high20 * (1 - DIP_ENTRY), close * 0.98))
            reason = f"Strong uptrend near its high — don't chase; buy on a dip to ₹{entry:,.2f}"
        else:
            action, entry_type, entry = "avoid", "hold", close
            reason = _avoid_reason(fr, key, p)

        plan = fr.plan_at(key, entry)
        score = _f(fr.score, key)
        daily = pd.DataFrame({"open": fr.o[key], "high": fr.h[key], "low": fr.l[key], "close": fr.c[key],
                              "volume": fr.v[key]}).dropna(subset=["close"])
        ch = chart.analyze(daily)
        rows.append({
            **base, "action": action, "reason": reason, "entry_type": entry_type,
            "close": round(close, 2), "score": round(score, 1) if np.isfinite(score) else 0.0,
            "entry": plan["entry"], "stop_loss": plan["stop"], "target": plan["target"],
            "reward_risk": plan["reward_risk"], "risk_per_share": plan["risk_per_share"],
            "stop_pct": round((plan["stop"] / plan["entry"] - 1) * 100, 1),
            "target_pct": round((plan["target"] / plan["entry"] - 1) * 100, 1),
            "entry_date": entry_date if action != "avoid" else None,
            "hold_until": exit_date if action == "buy" else None,
            "patterns": pat.names_on_bar(fr.patterns, key),
            "pullback_pct": round(pb * 100, 1) if np.isfinite(pb) else None,
            "rsi2": round(_f(fr.rsi2, key), 1), "ret_6": round(_f(fr.ret_6m, key) * 100, 1),
            "ret_1": round(_f(fr.ret_1m, key) * 100, 1), "atr_pct": round(_f(fr.atr_pct, key) * 100, 2),
            "vol_ratio": round(_f(fr.vol_ratio, key), 2), "sma50": round(_f(fr.sma50, key), 2),
            "sma200": round(_f(fr.sma200, key), 2), "turnover_cr": round(_f(fr.turnover_cr, key), 1),
            "hold_days": p.hold_days, "source": "swing" if action != "avoid" else None,
            "chart": {k: ch[k] for k in ("daily", "weekly", "volume", "patterns", "outlook", "outlook_score")},
            "spark": ch["spark"],
        })
        tradable = (close >= p.min_price and _f(fr.turnover_cr, key) >= p.min_turnover_cr
                    and _f(fr.atr_pct, key) <= p.max_atr_pct * 1.3 and len(daily) >= 120)
        apply_chart(rows[-1], ch, tradable, regime_ok, entry_date, high, _f(fr.atr, key))
    return [_clean(r) for r in rows]


def _set_plan(r: dict, entry: float, stop: float, target: float) -> None:
    risk = max(entry - stop, 0.01)
    r.update(entry=tick(entry), stop_loss=tick(stop), target=tick(target), risk_per_share=round(risk, 2),
             reward_risk=round((target - entry) / risk, 2), stop_pct=round((stop / entry - 1) * 100, 1),
             target_pct=round((target / entry - 1) * 100, 1))


def _desc(pt: dict) -> str:
    return f"{pt['label']} on the {pt['tf']} chart"


def apply_chart(r: dict, ch: dict, tradable: bool, regime_ok: bool, entry_date: str, today_high: float,
                atr: float) -> None:
    """Lets chart patterns upgrade or veto the swing verdict."""
    pats = ch["patterns"]
    bear = next((x for x in pats if x["bias"] == "bearish" and x["status"] == "breakdown"), None)
    if bear and r["action"] != "avoid":
        vol = f" on {bear['breakout_volume_x']}× average volume" if bear["confirmed"] else ""
        r.update(action="avoid", entry_type="hold", entry_date=None, hold_until=None, source="chart",
                 reason=f"{_desc(bear)} broke down{vol} — measured move points to ₹{bear['target']:,.2f}")
        return
    if r["action"] == "buy" or not tradable or ch["weekly"]["dir"] == "down":
        return
    bulls = [x for x in pats if x["bias"] == "bullish"]
    close = r["close"]
    max_stop = 0.12  # never risk more than 12% below the entry on a pattern trade

    bo = next((x for x in bulls if x["status"] == "breakout" and x["confirmed"]), None)
    if bo and regime_ok:
        stop = max(bo["stop"], close * (1 - max_stop))
        if bo["target"] > close and bo["target"] - close >= 1.5 * (close - stop):
            _set_plan(r, close, stop, bo["target"])
            exit_ = pd.Timestamp(entry_date) + pd.offsets.BDay(bo["hold_days"] - 1)
            r.update(action="buy", entry_type="open", entry_date=entry_date, hold_until=str(exit_.date()),
                     hold_days=bo["hold_days"], source="chart",
                     reason=f"{_desc(bo)} broke out above ₹{bo['pivot']:,.2f} on {bo['breakout_volume_x']}× average volume")
            return

    cand = bo or next((x for x in bulls if x["status"] == "breakout"), None) \
        or next((x for x in bulls if x["status"] == "forming" and x["volume_dry_up"]), None)
    if not cand:
        return
    trigger = max(cand["pivot"], today_high) if cand["status"] == "breakout" else cand["pivot"]
    entry = tick(trigger * 1.002)
    stop = max(cand["stop"], entry * (1 - max_stop))
    if stop >= entry or cand["target"] < entry * 1.03:
        return
    _set_plan(r, entry, stop, cand["target"])
    if cand["status"] == "breakout" and not regime_ok:
        why = f"{_desc(cand)} broke out, but the market is in a downtrend — buy only above ₹{entry:,.2f}"
    elif cand["status"] == "breakout":
        why = (f"{_desc(cand)} broke out on light volume ({cand['breakout_volume_x']}× average) — "
               f"buy only if it clears ₹{entry:,.2f} with stronger volume")
    else:
        why = f"{_desc(cand)} forming, volume drying up — buy on a breakout above ₹{entry:,.2f} on strong volume"
    r.update(action="wait", entry_type="above", entry_date=entry_date, hold_until=None,
             hold_days=cand["hold_days"], source="chart", reason=why)


def _clean(v):
    """NaN/inf are not valid JSON; send them as null, at any depth."""
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clean(x) for x in v]
    if isinstance(v, (float, np.floating)):
        return float(v) if np.isfinite(v) else None
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.bool_):
        return bool(v)
    return v


def finalize(rows: list[dict]) -> list[dict]:
    """Sorts buy > wait > avoid, best score first, and numbers the buys for grading."""
    rows.sort(key=lambda r: (ACTION_ORDER[r["action"]], -(r.get("score") or 0)))
    n = 0
    for r in rows:
        if r["action"] == "buy":
            n += 1
            r["rank"] = n
        else:
            r["rank"] = 0
        r["cum_rank"] = 0
    return rows
