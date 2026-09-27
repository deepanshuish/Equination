"""Monthly-rebalance backtest of the momentum rotation on cached candles.

Deliberately simple and honest: month-end signals, next-day execution is
approximated by trading at the month-end close with a round-trip cost, equal
weights, cash when the regime filter is off. It is a sanity check that the edge
exists in the data you actually trade, not a precise P&L simulator.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import indicators as ind
from .strategy import MIN_BARS, StrategyParams


def _monthly_stats(monthly: pd.Series) -> dict:
    m = monthly.dropna()
    if m.empty:
        return {}
    equity = (1 + m).cumprod()
    years = len(m) / 12
    cagr = equity.iloc[-1] ** (1 / years) - 1 if years > 0 else 0.0
    dd = (equity / equity.cummax() - 1).min()
    sharpe = m.mean() / m.std() * np.sqrt(12) if m.std() > 0 else 0.0
    return {
        "months": int(len(m)),
        "total_return_pct": round((equity.iloc[-1] - 1) * 100, 1),
        "cagr_pct": round(cagr * 100, 1),
        "avg_month_pct": round(m.mean() * 100, 2),
        "median_month_pct": round(m.median() * 100, 2),
        "best_month_pct": round(m.max() * 100, 1),
        "worst_month_pct": round(m.min() * 100, 1),
        "positive_months_pct": round((m > 0).mean() * 100, 0),
        "months_above_1pct": round((m > 0.01).mean() * 100, 0),
        "max_drawdown_pct": round(dd * 100, 1),
        "sharpe": round(float(sharpe), 2),
    }


def run_backtest(candles: pd.DataFrame, nifty: pd.Series, p: StrategyParams,
                 cost_pct: float = 0.25) -> dict:
    close = ind.to_wide(candles, "close")
    volume = ind.to_wide(candles, "volume")
    if close.empty or len(close) < MIN_BARS + 30:
        return {"error": "Not enough cached history to backtest. Refresh data first (needs > 1 year)."}

    nifty = nifty.reindex(close.index).ffill()
    regime_on = nifty > nifty.rolling(200).mean()

    ret_12_1 = ind.pct_return(close, 231, skip=21)
    ret_6 = ind.pct_return(close, 126)
    ret_3 = ind.pct_return(close, 63)
    ret_1 = ind.pct_return(close, 21)
    vol = ind.annualised_vol(close, 63)
    sma50 = ind.sma(close, 50)
    sma200 = ind.sma(close, 200)
    from_high = ind.pct_from_high(close, 252)
    turnover_cr = ind.avg_turnover(close, volume, 20) / 1e7
    bars = close.notna().cumsum()

    raw = p.w_12_1 * ret_12_1 + p.w_6 * ret_6 + p.w_3 * ret_3
    score = raw / vol.clip(lower=0.10)
    eligible = (
        (bars >= MIN_BARS) & (close >= p.min_price) & (turnover_cr >= p.min_turnover_cr)
        & (close > sma200) & (close > sma50) & (from_high >= p.max_from_high)
        & (vol <= p.max_vol) & (ret_1 <= p.max_1m_return) & (raw > 0)
    )
    score = score.where(eligible)

    month_ends = close.groupby(close.index.to_period("M")).tail(1).index
    month_ends = month_ends[month_ends >= close.index[MIN_BARS]]
    if len(month_ends) < 6:
        return {"error": "Fewer than 6 month-ends with a full year of history - refresh more data."}

    strat_rets: list[float] = []
    bench_rets: list[float] = []
    dates: list[str] = []
    turnover: list[float] = []
    prev_holdings: set[str] = set()
    for i in range(len(month_ends) - 1):
        d0, d1 = month_ends[i], month_ends[i + 1]
        row = score.loc[d0].dropna().sort_values(ascending=False)
        holdings = set(row.head(p.top_n).index) if bool(regime_on.loc[d0]) else set()
        period = close.loc[d0:d1]
        if holdings:
            leg = (period.iloc[-1][list(holdings)] / period.iloc[0][list(holdings)] - 1).fillna(0)
            gross = float(leg.mean()) * (len(holdings) / p.top_n)  # unfilled slots sit in cash
        else:
            gross = 0.0
        changed = len(holdings ^ prev_holdings) / max(p.top_n, 1)
        cost = changed * cost_pct / 100.0
        strat_rets.append(gross - cost)
        bench_rets.append(float(nifty.loc[d1] / nifty.loc[d0] - 1))
        dates.append(str(d1.date()))
        turnover.append(changed)
        prev_holdings = holdings

    strat = pd.Series(strat_rets, index=pd.to_datetime(dates))
    bench = pd.Series(bench_rets, index=pd.to_datetime(dates))
    eq_s = (1 + strat).cumprod()
    eq_b = (1 + bench).cumprod()
    return {
        "params": p.to_dict(),
        "cost_pct_per_switch": cost_pct,
        "period": {"from": str(month_ends[0].date()), "to": dates[-1]},
        "strategy": _monthly_stats(strat),
        "nifty": _monthly_stats(bench),
        "avg_monthly_turnover_pct": round(float(np.mean(turnover)) * 100, 0),
        "curve": [
            {"date": d, "strategy": round(float(s), 4), "nifty": round(float(b), 4), "month_ret": round(float(m) * 100, 2)}
            for d, s, b, m in zip(dates, eq_s, eq_b, strat)
        ],
    }
