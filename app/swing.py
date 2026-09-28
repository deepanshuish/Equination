"""4-day swing strategy: quality pullback in an uptrend + bullish reversal candle.

Setup (all on daily bars, evaluated at the close):
  Trend     : SMA50 > SMA200, close > SMA200, close >= 0.95 x SMA50, 6-month return > 0
  Pullback  : 3-12% below the 20-day high, and RSI(2) was < 25 on one of the last 3 bars
  Trigger   : a bullish candle today (morning star, engulfing, piercing, hammer,
              harami) or a strong high-volume close above yesterday's high
  Sanity    : ATR between 1% and 5% of price, liquid, not parabolic
  Regime    : Nifty 50 above its 200-day average

Trade plan: buy next open (approximated by today's close), stop below the
pattern low (max 2.5 ATR), target at the recent 10-day high or 2R, exit at the
close of the Nth day regardless. Fundamentals and promoter checks are applied
in the scanner as a gate on top of this setup.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from . import indicators as ind
from . import patterns as pat

MIN_BARS = 220


@dataclass
class SwingParams:
    hold_days: int = 4
    top_n: int = 10
    capital: float = 500_000.0
    risk_per_trade_pct: float = 1.0
    min_price: float = 50.0
    min_turnover_cr: float = 5.0
    min_pullback: float = 0.03
    max_pullback: float = 0.12
    rsi_threshold: float = 25.0
    min_atr_pct: float = 0.01
    max_atr_pct: float = 0.05
    max_stop_atr: float = 2.5
    cost_pct_round_trip: float = 0.15

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_settings(cls, s: dict[str, str]) -> "SwingParams":
        return cls(
            hold_days=max(1, int(float(s.get("hold_days", 4)))),
            top_n=int(float(s.get("top_n", 10))),
            capital=float(s.get("capital", 500_000)),
            risk_per_trade_pct=float(s.get("risk_per_trade_pct", 1.0)),
            min_price=float(s.get("min_price", 50)),
            min_turnover_cr=float(s.get("min_turnover_cr", 5)),
        )


def rsi(close: pd.DataFrame, n: int = 2) -> pd.DataFrame:
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    avg_up = up.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    avg_down = down.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    rs = avg_up / avg_down.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(avg_down != 0, 100.0)


class SwingFrames:
    """All indicator matrices needed for setups, computed once."""

    def __init__(self, candles: pd.DataFrame, p: SwingParams):
        self.p = p
        self.o = ind.to_wide(candles, "open")
        self.h = ind.to_wide(candles, "high")
        self.l = ind.to_wide(candles, "low")
        self.c = ind.to_wide(candles, "close")
        self.v = ind.to_wide(candles, "volume")
        c = self.c
        self.sma5 = ind.sma(c, 5)
        self.sma20 = ind.sma(c, 20)
        self.sma50 = ind.sma(c, 50)
        self.sma200 = ind.sma(c, 200)
        self.atr = ind.atr(self.h, self.l, c, 14)
        self.atr_pct = self.atr / c
        self.vol_avg = self.v.rolling(20, min_periods=20).mean()
        self.turnover_cr = ind.avg_turnover(c, self.v, 20) / 1e7
        self.high20 = self.h.rolling(20, min_periods=20).max()
        self.high10 = self.h.rolling(10, min_periods=10).max()
        self.pullback = 1 - c / self.high20
        self.rsi2 = rsi(c, 2)
        self.rsi_dip = (self.rsi2.shift(1) < p.rsi_threshold) | (self.rsi2.shift(2) < p.rsi_threshold) | (self.rsi2 < p.rsi_threshold)
        self.ret_6m = ind.pct_return(c, 126)
        self.ret_1m = ind.pct_return(c, 21)
        self.vol63 = ind.annualised_vol(c, 63)
        self.bars = c.notna().cumsum()
        self.patterns = pat.detect_all(self.o, self.h, self.l, c, self.v, self.vol_avg)
        self.pattern_strength = pat.strength(self.patterns)
        self.vol_ratio = self.v / self.vol_avg

        self.trend = (self.sma50 > self.sma200) & (c > self.sma200) & (c >= self.sma50 * 0.95) & (self.ret_6m > 0)
        self.sane = (
            (self.bars >= MIN_BARS) & (c >= p.min_price) & (self.turnover_cr >= p.min_turnover_cr)
            & (self.atr_pct >= p.min_atr_pct) & (self.atr_pct <= p.max_atr_pct) & (self.ret_1m <= 0.25)
        )
        self.pulled_back = (self.pullback >= p.min_pullback) & (self.pullback <= p.max_pullback) & self.rsi_dip
        self.trigger = self.pattern_strength > 0
        self.setup = self.trend & self.sane & self.pulled_back & self.trigger

        # Score: trend quality + pullback depth + pattern + volume, each as a cross-sectional rank.
        trend_q = (self.ret_6m / self.vol63.clip(lower=0.1)).rank(axis=1, pct=True)
        dip_q = self.rsi2.shift(1).rank(axis=1, pct=True, ascending=False)
        patt = self.pattern_strength / 3.0
        volq = self.vol_ratio.clip(upper=3).rank(axis=1, pct=True)
        self.score = (0.35 * trend_q + 0.20 * dip_q.fillna(0.5) + 0.30 * patt + 0.15 * volq.fillna(0.5)) * 100

    # ------------------------------------------------------------ trade plan
    def plan(self, symbol: str, i: int | None = None) -> dict:
        """Entry/stop/target for `symbol` on bar index i (default: last bar)."""
        i = len(self.c) - 1 if i is None else i
        close = float(self.c[symbol].iloc[i])
        atr = float(self.atr[symbol].iloc[i])
        pattern_low = float(self.l[symbol].iloc[max(i - 2, 0): i + 1].min())
        stop = max(pattern_low - 0.25 * atr, close - self.p.max_stop_atr * atr)
        risk = max(close - stop, 0.01)
        hi10 = float(self.high10[symbol].iloc[i])
        target = hi10 if hi10 >= close + risk else close + 2 * risk
        return {"entry": round(close, 2), "stop": round(stop, 2), "target": round(target, 2),
                "risk_per_share": round(risk, 2), "reward_risk": round((target - close) / risk, 2)}


def regime_on(nifty: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    n = nifty.reindex(index).ffill()
    return n > n.rolling(200, min_periods=150).mean()


def latest_candidates(fr: SwingFrames, regime_ok: bool) -> pd.DataFrame:
    """Setup rows on the last bar, sorted by score, with the reasons a stock failed."""
    last = fr.c.index[-1]
    rows = []
    for sym in fr.c.columns:
        if pd.isna(fr.c[sym].iloc[-1]):
            continue
        why = []
        if not bool(fr.sane[sym].iloc[-1]):
            if fr.bars[sym].iloc[-1] < MIN_BARS:
                why.append("insufficient history")
            elif fr.c[sym].iloc[-1] < fr.p.min_price:
                why.append("price too low")
            elif not (fr.turnover_cr[sym].iloc[-1] >= fr.p.min_turnover_cr):
                why.append("illiquid")
            elif fr.ret_1m[sym].iloc[-1] > 0.25:
                why.append("parabolic last month")
            else:
                why.append("volatility out of range")
        if not bool(fr.trend[sym].iloc[-1]):
            why.append("not in uptrend")
        if not bool(fr.pulled_back[sym].iloc[-1]):
            why.append("no pullback")
        if not bool(fr.trigger[sym].iloc[-1]):
            why.append("no reversal candle")
        rows.append({
            "instrument_key": sym,
            "setup": not why,
            "excluded": "; ".join(why),
            "score": float(fr.score[sym].iloc[-1]) if pd.notna(fr.score[sym].iloc[-1]) else 0.0,
            "close": float(fr.c[sym].iloc[-1]),
            "patterns": pat.names_on_bar(fr.patterns, sym),
            "pullback_pct": round(float(fr.pullback[sym].iloc[-1]) * 100, 1),
            "rsi2": round(float(fr.rsi2[sym].iloc[-1]), 1),
            "rsi2_prev": round(float(fr.rsi2[sym].iloc[-2]), 1),
            "ret_6m": round(float(fr.ret_6m[sym].iloc[-1]) * 100, 1),
            "ret_1m": round(float(fr.ret_1m[sym].iloc[-1]) * 100, 1),
            "atr_pct": round(float(fr.atr_pct[sym].iloc[-1]) * 100, 2),
            "vol_ratio": round(float(fr.vol_ratio[sym].iloc[-1]), 2),
            "sma50": round(float(fr.sma50[sym].iloc[-1]), 2),
            "sma200": round(float(fr.sma200[sym].iloc[-1]), 2),
            "turnover_cr": round(float(fr.turnover_cr[sym].iloc[-1]), 1),
            "as_of": str(last.date()),
            "regime_ok": regime_ok,
        })
    df = pd.DataFrame(rows).set_index("instrument_key")
    return df.sort_values("score", ascending=False)


def size_position(entry: float, stop: float, p: SwingParams, allocation: float) -> tuple[int, float, float]:
    risk_amt = p.capital * p.risk_per_trade_pct / 100.0
    per_slot = p.capital * allocation / max(p.top_n, 1)
    rps = max(entry - stop, 0.01)
    qty = max(min(int(risk_amt // rps), int(per_slot // entry) if entry > 0 else 0), 0)
    return qty, round(qty * entry, 0), round(qty * rps, 0)


# ------------------------------------------------------------------ backtest
def backtest(candles: pd.DataFrame, nifty: pd.Series, p: SwingParams, max_trades_per_day: int | None = None) -> dict:
    fr = SwingFrames(candles, p)
    if len(fr.c) < MIN_BARS + p.hold_days + 5:
        return {"error": "Not enough cached history to backtest the swing setup (need > 1 year)."}
    reg = regime_on(nifty, fr.c.index)
    setup = fr.setup & reg.values[:, None]
    O, H, L, C = fr.o.values, fr.h.values, fr.l.values, fr.c.values
    dates = fr.c.index
    syms = list(fr.c.columns)
    n_days, n_syms = C.shape
    cost = p.cost_pct_round_trip / 100.0

    trades: list[dict] = []
    for i in range(MIN_BARS, n_days - 1):
        cols = np.where(setup.values[i])[0]
        if len(cols) == 0:
            continue
        if max_trades_per_day:
            order = np.argsort(-fr.score.values[i, cols])
            cols = cols[order][:max_trades_per_day]
        for j in cols:
            plan = fr.plan(syms[j], i)
            entry = O[i + 1, j] if not np.isnan(O[i + 1, j]) else C[i, j]
            if np.isnan(entry):
                continue
            stop, target = plan["stop"], plan["target"]
            exit_px, exit_reason, exit_i = None, "time", min(i + p.hold_days, n_days - 1)
            for k in range(i + 1, min(i + p.hold_days, n_days - 1) + 1):
                if np.isnan(L[k, j]):
                    continue
                if k > i + 1 and O[k, j] <= stop:  # gap through the stop: filled at the open
                    exit_px, exit_reason, exit_i = O[k, j], "stop", k
                    break
                if L[k, j] <= stop:
                    exit_px, exit_reason, exit_i = stop, "stop", k
                    break
                if H[k, j] >= target:
                    exit_px, exit_reason, exit_i = target, "target", k
                    break
            if exit_px is None:
                exit_px = C[exit_i, j]
            ret = exit_px / entry - 1 - cost
            trades.append({"date": str(dates[i].date()), "symbol": syms[j], "ret": ret, "exit": exit_reason,
                           "days": exit_i - i, "score": float(fr.score.values[i, j])})

    if not trades:
        return {"error": "No setups found in the cached history."}
    t = pd.DataFrame(trades)
    wins = t["ret"] > 0
    gross_win = t.loc[wins, "ret"].sum()
    gross_loss = -t.loc[~wins, "ret"].sum()

    # Baseline: every trend-eligible stock held for the same number of days on the same dates, no setup required.
    fwd = fr.c.shift(-p.hold_days) / fr.o.shift(-1) - 1 - cost
    base_mask = (fr.trend & fr.sane) & reg.values[:, None]
    base = fwd.where(base_mask).stack().dropna()
    by_year = t.assign(year=t["date"].str[:4]).groupby("year")["ret"].agg(["count", "mean", lambda s: (s > 0).mean()])
    by_year.columns = ["trades", "avg_ret", "win_rate"]
    by_exit = t.groupby("exit")["ret"].agg(["count", "mean"])
    return {
        "params": p.to_dict(),
        "period": {"from": t["date"].min(), "to": t["date"].max()},
        "trades": int(len(t)),
        "win_rate_pct": round(float(wins.mean()) * 100, 1),
        "avg_return_pct": round(float(t["ret"].mean()) * 100, 2),
        "median_return_pct": round(float(t["ret"].median()) * 100, 2),
        "avg_win_pct": round(float(t.loc[wins, "ret"].mean()) * 100, 2) if wins.any() else 0.0,
        "avg_loss_pct": round(float(t.loc[~wins, "ret"].mean()) * 100, 2) if (~wins).any() else 0.0,
        "profit_factor": round(float(gross_win / gross_loss), 2) if gross_loss > 0 else None,
        "avg_days_held": round(float(t["days"].mean()), 1),
        "baseline_avg_return_pct": round(float(base.mean()) * 100, 2) if len(base) else None,
        "baseline_win_rate_pct": round(float((base > 0).mean()) * 100, 1) if len(base) else None,
        "by_exit": {k: {"trades": int(r["count"]), "avg_ret_pct": round(float(r["mean"]) * 100, 2)} for k, r in by_exit.iterrows()},
        "by_year": [{"year": y, "trades": int(r["trades"]), "avg_ret_pct": round(float(r["avg_ret"]) * 100, 2),
                     "win_rate_pct": round(float(r["win_rate"]) * 100, 1)} for y, r in by_year.iterrows()],
        "top_score_bucket": _score_buckets(t),
        "recent_trades": t.sort_values("date", ascending=False).head(30).assign(ret=lambda d: (d["ret"] * 100).round(2)).to_dict("records"),
    }


def _score_buckets(t: pd.DataFrame) -> list[dict]:
    try:
        t = t.assign(bucket=pd.qcut(t["score"], 3, labels=["low", "mid", "high"], duplicates="drop"))
    except ValueError:
        return []
    g = t.groupby("bucket", observed=True)["ret"].agg(["count", "mean", lambda s: (s > 0).mean()])
    return [{"bucket": str(b), "trades": int(r["count"]), "avg_ret_pct": round(float(r["mean"]) * 100, 2),
             "win_rate_pct": round(float(r.iloc[2]) * 100, 1)} for b, r in g.iterrows()]
