"""Vectorised indicator helpers on wide (date x symbol) frames."""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(close: pd.DataFrame, n: int) -> pd.DataFrame:
    return close.rolling(n, min_periods=n).mean()


def pct_return(close: pd.DataFrame, n: int, skip: int = 0) -> pd.DataFrame:
    """Return over the last `n` bars, ending `skip` bars ago (e.g. 12-1 momentum)."""
    end = close.shift(skip)
    start = close.shift(skip + n)
    return end / start - 1.0


def annualised_vol(close: pd.DataFrame, n: int) -> pd.DataFrame:
    return np.log(close / close.shift(1)).rolling(n, min_periods=n).std() * np.sqrt(252)


def atr(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    prev = close.shift(1)
    tr = np.maximum(high - low, np.maximum((high - prev).abs(), (low - prev).abs()))
    return tr.rolling(n, min_periods=n).mean()


def pct_from_high(close: pd.DataFrame, n: int = 252) -> pd.DataFrame:
    return close / close.rolling(n, min_periods=int(n * 0.8)).max() - 1.0


def avg_turnover(close: pd.DataFrame, volume: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    return (close * volume).rolling(n, min_periods=n).mean()


def max_drawdown(close: pd.DataFrame, n: int) -> pd.DataFrame:
    roll_max = close.rolling(n, min_periods=int(n * 0.8)).max()
    return (close / roll_max - 1.0).rolling(n, min_periods=int(n * 0.8)).min()


def to_wide(long_df: pd.DataFrame, field: str) -> pd.DataFrame:
    wide = long_df.pivot(index="date", columns="instrument_key", values=field).sort_index()
    return wide.astype(float)
