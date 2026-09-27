"""Trend-filtered, volatility-adjusted momentum rotation.

Why this strategy
-----------------
Cross-sectional momentum (buy the strongest 12-month performers, skipping the
most recent month) is the most robust anomaly in the academic literature and has
worked in Indian equities for decades. Two additions materially improve its
live behaviour:

* A market regime filter (Nifty 50 above its 200-day average) keeps the
  portfolio out of the worst bear markets, where momentum "crashes".
* Dividing raw momentum by realised volatility prefers smooth up-trends over
  parabolic ones, cutting drawdown at little cost to return.

The result is a positional (not intraday) system that is reviewed daily but
typically trades only a handful of times a month. Targeting 1-3% a month on
average - with losing months - is the realistic expectation, and the built-in
backtest lets you check that against real Upstox data rather than trust it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from . import indicators as ind

MIN_BARS = 260  # need a full year of history plus the 1-month skip


@dataclass
class StrategyParams:
    top_n: int = 10
    capital: float = 500_000.0
    risk_per_trade_pct: float = 1.0
    min_price: float = 50.0
    min_turnover_cr: float = 5.0  # 20-day average traded value, in crore
    max_vol: float = 0.60  # annualised, filters out lottery tickets
    max_from_high: float = -0.25  # must be within 25% of its 52-week high
    max_1m_return: float = 0.35  # skip stocks that already went parabolic this month
    atr_stop_mult: float = 2.5
    w_12_1: float = 0.40
    w_6: float = 0.35
    w_3: float = 0.25

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_settings(cls, s: dict[str, str]) -> "StrategyParams":
        return cls(
            top_n=int(float(s.get("top_n", 10))),
            capital=float(s.get("capital", 500_000)),
            risk_per_trade_pct=float(s.get("risk_per_trade_pct", 1.0)),
            min_price=float(s.get("min_price", 50)),
            min_turnover_cr=float(s.get("min_turnover_cr", 5)),
        )


@dataclass
class Regime:
    state: str  # RISK_ON | CAUTION | RISK_OFF | UNKNOWN
    nifty_close: float | None
    sma50: float | None
    sma200: float | None
    allocation: float  # fraction of capital the strategy recommends deploying
    note: str

    def to_dict(self) -> dict:
        return asdict(self)


def market_regime(nifty_close: pd.Series) -> Regime:
    s = nifty_close.dropna()
    if len(s) < 200:
        return Regime("UNKNOWN", None, None, None, 0.5, "Not enough Nifty history to judge the regime.")
    close = float(s.iloc[-1])
    sma50 = float(s.rolling(50).mean().iloc[-1])
    sma200 = float(s.rolling(200).mean().iloc[-1])
    if close > sma200 and close > sma50:
        return Regime("RISK_ON", close, sma50, sma200, 1.0,
                      "Nifty 50 is above its 50 and 200-day averages: deploy full allocation.")
    if close > sma200:
        return Regime("CAUTION", close, sma50, sma200, 0.5,
                      "Nifty 50 is above its 200-day average but below the 50-day: deploy half, tighten stops.")
    return Regime("RISK_OFF", close, sma50, sma200, 0.0,
                  "Nifty 50 is below its 200-day average: momentum historically underperforms here - stay in cash "
                  "or hold only existing positions above their stops.")


def compute_features(candles: pd.DataFrame) -> pd.DataFrame:
    """Latest-bar feature table (one row per instrument) from long-format candles."""
    close = ind.to_wide(candles, "close")
    high = ind.to_wide(candles, "high")
    low = ind.to_wide(candles, "low")
    volume = ind.to_wide(candles, "volume")

    feats = pd.DataFrame(index=close.columns)
    feats["bars"] = close.notna().sum()
    feats["last_date"] = close.apply(lambda c: c.last_valid_index())
    feats["close"] = close.ffill().iloc[-1]
    feats["ret_12_1"] = ind.pct_return(close, 231, skip=21).iloc[-1]
    feats["ret_6"] = ind.pct_return(close, 126).iloc[-1]
    feats["ret_3"] = ind.pct_return(close, 63).iloc[-1]
    feats["ret_1"] = ind.pct_return(close, 21).iloc[-1]
    feats["vol"] = ind.annualised_vol(close, 63).iloc[-1]
    feats["sma50"] = ind.sma(close, 50).iloc[-1]
    feats["sma200"] = ind.sma(close, 200).iloc[-1]
    feats["from_high"] = ind.pct_from_high(close, 252).iloc[-1]
    feats["atr"] = ind.atr(high, low, close, 14).iloc[-1]
    feats["turnover_cr"] = ind.avg_turnover(close, volume, 20).iloc[-1] / 1e7
    feats["dd_6m"] = ind.max_drawdown(close, 126).iloc[-1]
    return feats


def score_features(feats: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    f = feats.copy()
    raw = p.w_12_1 * f["ret_12_1"] + p.w_6 * f["ret_6"] + p.w_3 * f["ret_3"]
    f["raw_momentum"] = raw
    f["score"] = raw / f["vol"].clip(lower=0.10)

    reasons = []
    for key, r in f.iterrows():
        why = []
        if r["bars"] < MIN_BARS:
            why.append("insufficient history")
        if r["close"] < p.min_price:
            why.append(f"price < {p.min_price:g}")
        if not (r["turnover_cr"] >= p.min_turnover_cr):
            why.append(f"turnover < {p.min_turnover_cr:g} cr")
        if not (r["close"] > r["sma200"]):
            why.append("below 200 DMA")
        if not (r["close"] > r["sma50"]):
            why.append("below 50 DMA")
        if not (r["from_high"] >= p.max_from_high):
            why.append("far from 52w high")
        if not (r["vol"] <= p.max_vol):
            why.append("too volatile")
        if r["ret_1"] > p.max_1m_return:
            why.append("parabolic last month")
        if not (r["raw_momentum"] > 0):
            why.append("negative momentum")
        reasons.append("; ".join(why))
    f["excluded"] = reasons
    f["eligible"] = f["excluded"] == ""
    f = f.sort_values("score", ascending=False)
    f["rank"] = np.where(f["eligible"], f["eligible"].cumsum(), 0)
    return f


def build_picks(scored: pd.DataFrame, instruments: pd.DataFrame, p: StrategyParams, regime: Regime) -> list[dict]:
    names = instruments.set_index("instrument_key")
    picks = scored[scored["eligible"]].head(p.top_n)
    deployable = p.capital * regime.allocation
    per_slot = deployable / max(p.top_n, 1)
    risk_amt = p.capital * p.risk_per_trade_pct / 100.0
    out: list[dict] = []
    for key, r in picks.iterrows():
        close = float(r["close"])
        stop = round(max(close - p.atr_stop_mult * float(r["atr"]), 0.01), 2)
        risk_per_share = max(close - stop, 0.01)
        qty_by_risk = int(risk_amt // risk_per_share)
        qty_by_slot = int(per_slot // close) if close > 0 else 0
        qty = max(min(qty_by_risk, qty_by_slot), 0)
        out.append({
            "rank": int(r["rank"]),
            "instrument_key": key,
            "symbol": names["symbol"].get(key, key),
            "name": names["name"].get(key, ""),
            "close": round(close, 2),
            "as_of": str(pd.Timestamp(r["last_date"]).date()) if pd.notna(r["last_date"]) else None,
            "score": round(float(r["score"]), 3),
            "ret_12_1": round(float(r["ret_12_1"]) * 100, 1),
            "ret_6": round(float(r["ret_6"]) * 100, 1),
            "ret_3": round(float(r["ret_3"]) * 100, 1),
            "ret_1": round(float(r["ret_1"]) * 100, 1),
            "vol": round(float(r["vol"]) * 100, 1),
            "from_high": round(float(r["from_high"]) * 100, 1),
            "sma50": round(float(r["sma50"]), 2),
            "sma200": round(float(r["sma200"]), 2),
            "atr": round(float(r["atr"]), 2),
            "turnover_cr": round(float(r["turnover_cr"]), 1),
            "stop_loss": stop,
            "quantity": qty,
            "position_value": round(qty * close, 0),
            "risk_amount": round(qty * risk_per_share, 0),
            "exit_rules": "Exit if close < stop, or close < 50 DMA, or stock drops out of the top "
                          f"{p.top_n * 2} ranks at the monthly review.",
        })
    return out


def eligibility_summary(scored: pd.DataFrame) -> dict:
    return {
        "universe": int(len(scored)),
        "eligible": int(scored["eligible"].sum()),
        "excluded_reasons": scored.loc[~scored["eligible"], "excluded"]
        .str.split("; ").explode().value_counts().head(8).to_dict(),
    }
