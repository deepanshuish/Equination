"""Bullish candlestick pattern detection on wide (date x symbol) OHLCV frames.

Every function returns a boolean DataFrame aligned with the inputs; True on the
bar that completes the pattern. Definitions follow the common textbook forms
(Nison) with a small tolerance so that real-world candles qualify.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

PATTERN_STRENGTH = {
    "morning_star": 3,
    "bullish_engulfing": 3,
    "piercing": 2,
    "hammer": 2,
    "bullish_harami": 1,
    "strong_close": 1,
}


def _body(o, c):
    return (c - o).abs()


def _range(h, l):
    return (h - l).replace(0, np.nan)


def hammer(o, h, l, c) -> pd.DataFrame:
    body = _body(o, c)
    rng = _range(h, l)
    lower = np.minimum(o, c) - l
    upper = h - np.maximum(o, c)
    return (body <= rng * 0.35) & (lower >= body * 2) & (lower >= rng * 0.6) & (upper <= rng * 0.15) & (body > 0) & (c >= o * 0.995)


def bullish_engulfing(o, h, l, c) -> pd.DataFrame:
    po, pc = o.shift(1), c.shift(1)
    prev_red = pc < po
    return prev_red & (c > o) & (o <= pc) & (c >= po) & (_body(o, c) > _body(po, pc) * 1.05)


def piercing(o, h, l, c) -> pd.DataFrame:
    po, pc = o.shift(1), c.shift(1)
    mid = (po + pc) / 2
    return (pc < po) & (c > o) & (o < pc) & (c > mid) & (c < po)


def morning_star(o, h, l, c) -> pd.DataFrame:
    o1, c1 = o.shift(2), c.shift(2)  # big red
    o2, c2 = o.shift(1), c.shift(1)  # small body
    body1, body2, body3 = _body(o1, c1), _body(o2, c2), _body(o, c)
    return (c1 < o1) & (body2 <= body1 * 0.4) & (c > o) & (body3 >= body1 * 0.5) & (c > (o1 + c1) / 2)


def bullish_harami(o, h, l, c) -> pd.DataFrame:
    po, pc = o.shift(1), c.shift(1)
    return (pc < po) & (c > o) & (o > pc) & (c < po) & (_body(o, c) <= _body(po, pc) * 0.6)


def strong_close(o, h, l, c, v, vol_avg) -> pd.DataFrame:
    """Green candle closing in the top quarter of its range on above-average volume,
    taking out the previous bar's high."""
    rng = _range(h, l)
    return (c > o) & ((c - l) / rng >= 0.75) & (c > h.shift(1)) & (v >= vol_avg * 1.2)


def detect_all(o, h, l, c, v, vol_avg) -> dict[str, pd.DataFrame]:
    return {
        "morning_star": morning_star(o, h, l, c),
        "bullish_engulfing": bullish_engulfing(o, h, l, c),
        "piercing": piercing(o, h, l, c),
        "hammer": hammer(o, h, l, c),
        "bullish_harami": bullish_harami(o, h, l, c),
        "strong_close": strong_close(o, h, l, c, v, vol_avg),
    }


def strength(patterns: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Max strength among the patterns present on each bar (0 when none)."""
    out = None
    for name, mask in patterns.items():
        s = mask.astype(float) * PATTERN_STRENGTH[name]
        out = s if out is None else np.maximum(out, s)
    return out.fillna(0)


def names_on_bar(patterns: dict[str, pd.DataFrame], symbol: str) -> list[str]:
    return [name for name, mask in patterns.items() if bool(mask[symbol].iloc[-1])]
