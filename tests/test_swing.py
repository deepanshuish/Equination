"""Candle patterns, swing setup/backtest and fundamentals gates on synthetic data."""
import os
import tempfile

import numpy as np
import pandas as pd

os.environ.setdefault("EQUINATION_DATA_DIR", tempfile.mkdtemp())

from app import fundamentals as fnd  # noqa: E402
from app import patterns as pat  # noqa: E402
from app import swing  # noqa: E402


def _wide(rows):
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])
    df.index = pd.bdate_range("2024-01-01", periods=len(df))
    return {k: df[[k]].rename(columns={k: "X"}) for k in df.columns}


def test_patterns_detect_textbook_shapes():
    w = _wide([
        [100, 101, 99, 100, 1000],
        [100, 101, 95, 96, 1000],     # red
        [95, 101, 94.5, 100.5, 1500],  # engulfs previous body
    ])
    assert bool(pat.bullish_engulfing(w["open"], w["high"], w["low"], w["close"]).iloc[-1, 0])
    w = _wide([[100, 101, 99, 100, 1], [100, 100.5, 94, 100.2, 1]])  # long lower shadow, tiny body
    assert bool(pat.hammer(w["open"], w["high"], w["low"], w["close"]).iloc[-1, 0])
    w = _wide([[100, 101, 99, 100, 1], [100, 101, 94, 95, 1], [93, 99, 92.5, 98.5, 1]])  # opens below, closes above mid
    assert bool(pat.piercing(w["open"], w["high"], w["low"], w["close"]).iloc[-1, 0])
    w = _wide([[100, 101, 99, 100, 1], [100, 101, 93, 94, 1], [93.5, 94.5, 92.5, 93.8, 1], [94, 99.5, 93.8, 99, 1]])
    assert bool(pat.morning_star(w["open"], w["high"], w["low"], w["close"]).iloc[-1, 0])


def make_uptrend_with_pullback(n=320, seed=3):
    """Smooth uptrend, then a 5-day dip, then a bullish engulfing candle on the last bar."""
    rng = np.random.default_rng(seed)
    close = 100 * np.cumprod(1 + rng.normal(0.0012, 0.008, n))
    close[-6:-1] = close[-7] * np.array([0.99, 0.975, 0.96, 0.95, 0.945])  # ~5.5% pullback
    o = close.copy(); h = close * 1.006; l = close * 0.994
    o[-6:-1] = close[-7:-2] * 1.0
    # last bar: opens below previous close, closes above previous open -> engulfing
    o[-1] = close[-2] * 0.995
    close[-1] = o[-2] * 1.01
    h[-1] = close[-1] * 1.002
    l[-1] = o[-1] * 0.998
    v = np.full(n, 1_000_000.0); v[-1] = 1_600_000
    dates = pd.bdate_range(end="2025-06-30", periods=n)
    return pd.DataFrame({"instrument_key": "NSE_EQ|UP", "date": dates, "open": o, "high": h, "low": l,
                         "close": close, "volume": v})


def test_swing_setup_fires_on_pullback_reversal():
    df = make_uptrend_with_pullback()
    flat = df.copy(); flat["instrument_key"] = "NSE_EQ|FLAT"
    for c in ("open", "high", "low", "close"):
        flat[c] = 200.0
    candles = pd.concat([df, flat], ignore_index=True)
    p = swing.SwingParams()
    fr = swing.SwingFrames(candles, p)
    cands = swing.latest_candidates(fr, True)
    assert bool(cands.loc["NSE_EQ|UP", "setup"]), cands.loc["NSE_EQ|UP", "excluded"]
    assert "bullish_engulfing" in cands.loc["NSE_EQ|UP", "patterns"]
    assert not bool(cands.loc["NSE_EQ|FLAT", "setup"])
    plan = fr.plan("NSE_EQ|UP")
    assert plan["stop"] < plan["entry"] < plan["target"]
    qty, value, risk = swing.size_position(plan["entry"], plan["stop"], p, 1.0)
    assert qty > 0 and risk <= p.capital * p.risk_per_trade_pct / 100 + plan["entry"]


def test_swing_backtest_runs():
    rng = np.random.default_rng(7)
    frames = []
    dates = pd.bdate_range(end="2025-06-30", periods=700)
    for i in range(6):
        close = 100 * np.cumprod(1 + rng.normal(0.0008, 0.015, 700))
        o = close * (1 + rng.normal(0, 0.004, 700))
        h = np.maximum(o, close) * (1 + abs(rng.normal(0, 0.006, 700)))
        l = np.minimum(o, close) * (1 - abs(rng.normal(0, 0.006, 700)))
        frames.append(pd.DataFrame({"instrument_key": f"NSE_EQ|S{i}", "date": dates, "open": o, "high": h, "low": l,
                                    "close": close, "volume": rng.uniform(5e5, 3e6, 700)}))
    candles = pd.concat(frames, ignore_index=True)
    nifty = candles[candles["instrument_key"] == "NSE_EQ|S0"].set_index("date")["close"]
    res = swing.backtest(candles, nifty, swing.SwingParams())
    assert "error" not in res or "No setups" in res["error"]
    if "error" not in res:
        assert res["trades"] > 0 and 0 <= res["win_rate_pct"] <= 100
        assert len(res["by_year"]) >= 1


def test_fundamentals_parsers_and_gate():
    ratios = [
        {"name": "Return on Equity (%)", "company_value": "18.4", "sector_value": "12"},
        {"name": "P/E Ratio", "company_value": 32.1},
        {"name": "Debt to Equity", "company_value": "0.35"},
        {"name": "Net Profit Margin (%)", "company_value": "11.2"},
        {"name": "EPS Growth (%)", "company_value": "14"},
    ]
    holdings = [
        {"category": "Promoters", "history": {"Jun 2024": "52.1", "Sep 2024": 52.1, "Dec 2024": "51.9", "Mar 2025": "51.8"}},
        {"category": "FII", "history": [{"period": "Mar 2025", "value": "18.2"}]},
        {"category": "Pledged (Promoter)", "history": {"Mar 2025": 0}},
    ]
    f = fnd.build("INE000", ratios, holdings, "Good Ltd")
    assert f.ratios["roe"] == 18.4 and f.ratios["pe"] == 32.1 and f.ratios["debt_equity"] == 0.35
    assert f.promoter_pct == 51.8 and f.promoter_change_pp == -0.3 and f.fii_pct == 18.2 and f.pledge_pct == 0
    a = fnd.assess(f, fnd.QualityParams())
    assert a["ok"] and a["score"] > 50 and not a["hard"]

    # Promoters dumping stock + pledges -> excluded
    bad_hold = [{"category": "Promoter & Promoter Group", "history": {"Sep 2024": 60, "Dec 2024": 58, "Mar 2025": 55}},
                {"category": "Promoter pledge %", "history": {"Mar 2025": 40}}]
    fb = fnd.build("INE001", ratios, bad_hold, "Shady Ltd")
    ab = fnd.assess(fb, fnd.QualityParams())
    assert not ab["ok"] and any("cut stake" in h for h in ab["hard"]) and any("pledged" in h for h in ab["hard"])

    # Loss-making -> excluded; bank with high D/E -> allowed
    loss = fnd.build("INE002", [{"name": "P/E", "company_value": "-5"}], None, "Loss Ltd")
    assert not fnd.assess(loss, fnd.QualityParams())["ok"]
    bank = fnd.build("INE003", [{"name": "Debt to Equity", "company_value": "8"}, {"name": "ROE", "company_value": "15"}], None, "Big Bank Ltd")
    assert fnd.assess(bank, fnd.QualityParams())["ok"]

    # No data: flagged by default, excluded when required
    assert fnd.assess(None, fnd.QualityParams())["ok"]
    assert not fnd.assess(None, fnd.QualityParams(require_fundamentals=True))["ok"]
