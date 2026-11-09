"""Every stock gets an action with a valid entry, stop-loss and target."""
import os
import tempfile

import numpy as np
import pandas as pd

os.environ.setdefault("EQUINATION_DATA_DIR", tempfile.mkdtemp())

from app import overview, swing  # noqa: E402
from tests.test_swing import make_uptrend_with_pullback  # noqa: E402


def _series(key, close):
    n = len(close)
    return pd.DataFrame({"instrument_key": key, "date": pd.bdate_range(end="2025-06-30", periods=n),
                         "open": close, "high": close * 1.006, "low": close * 0.994, "close": close,
                         "volume": np.full(n, 1_000_000.0)})


def test_build_rates_every_stock():
    rng = np.random.default_rng(5)
    n = 320
    up = make_uptrend_with_pullback(n)
    steady = _series("NSE_EQ|STEADY", 100 * np.cumprod(1 + rng.normal(0.0015, 0.012, n)))
    down = _series("NSE_EQ|DOWN", 300 * np.cumprod(1 + rng.normal(-0.002, 0.012, n)))
    candles = pd.concat([up, steady, down], ignore_index=True)
    names = pd.DataFrame({"instrument_key": ["NSE_EQ|UP", "NSE_EQ|STEADY", "NSE_EQ|DOWN", "NSE_EQ|NODATA"],
                          "symbol": ["UP", "STEADY", "DOWN", "NODATA"], "name": ["", "", "", ""]}).set_index("instrument_key")
    p = swing.SwingParams()
    rows = overview.finalize(overview.build(swing.SwingFrames(candles, p), names, p, True, "2025-07-01", "2025-07-04"))
    by = {r["symbol"]: r for r in rows}
    assert len(rows) == 4 and rows[0]["symbol"] == "UP"
    assert by["UP"]["action"] == "buy" and by["UP"]["entry_type"] == "open" and by["UP"]["rank"] == 1
    assert by["UP"]["hold_until"] == "2025-07-04"
    assert by["DOWN"]["action"] == "avoid" and by["DOWN"]["rank"] == 0
    assert by["NODATA"]["action"] == "avoid" and by["NODATA"]["entry"] is None
    for r in rows:
        if r["entry"] is not None:
            assert r["stop_loss"] < r["entry"] < r["target"], r

    # market in a downtrend: the setup becomes a wait above today's high
    rows = overview.build(swing.SwingFrames(candles, p), names, p, False, "2025-07-01", "2025-07-04")
    up_row = next(r for r in rows if r["symbol"] == "UP")
    assert up_row["action"] == "wait" and up_row["entry_type"] == "above" and up_row["entry"] > up_row["close"]
