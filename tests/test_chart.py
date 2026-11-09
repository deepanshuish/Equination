"""Chart reader on textbook shapes: cup & handle, bull flag, bear flag, trend, volume."""
import numpy as np
import pandas as pd

from app import chart


def _frame(close, vol, wiggle=0.004):
    close = np.asarray(close, float)
    idx = pd.bdate_range(end="2025-06-27", periods=len(close))  # ends on a Friday
    return pd.DataFrame({"open": np.r_[close[0], close[:-1]], "high": close * (1 + wiggle), "low": close * (1 - wiggle),
                         "close": close, "volume": np.asarray(vol, float)}, index=idx)


def cup_and_handle(breakout=True):
    rng = np.random.default_rng(1)
    advance = np.linspace(100, 150, 80)
    cup = 150 - 35 * np.sin(np.linspace(0, np.pi, 70))           # 150 -> 115 -> 150, rounded
    handle = np.linspace(149, 141, 9)                              # ~6% shallow pullback
    close = np.r_[advance, cup, handle, [153.0 if breakout else 142.0]] * (1 + rng.normal(0, 0.002, 160))
    vol = np.r_[np.full(80, 1.2e6), np.full(70, 1.0e6), np.full(9, 0.5e6), [2.4e6 if breakout else 0.6e6]]
    return _frame(close, vol)


def bull_flag(breakout=True):
    rng = np.random.default_rng(2)
    base = 100 * np.cumprod(1 + rng.normal(0.001, 0.006, 150))
    pole = base[-1] * np.linspace(1.0, 1.18, 7)[1:]               # +18% in 6 days
    flag = pole[-1] * np.linspace(0.99, 0.95, 6)                   # small orderly drift down
    last = [pole[-1] * 1.02 if breakout else flag[-1] * 1.003]
    close = np.r_[base, pole, flag, last]
    vol = np.r_[np.full(150, 1e6), np.full(6, 2.5e6), np.full(6, 0.8e6), [2.0e6 if breakout else 0.7e6]]
    return _frame(close, vol)


def test_cup_and_handle_breakout_and_forming():
    p = chart.cup_handle(cup_and_handle(True), chart.DAILY)
    assert p and p["kind"] == "cup_handle" and p["status"] == "breakout" and p["confirmed"], p
    assert p["volume_dry_up"] and p["stop"] < p["pivot"] < p["target"]
    f = chart.cup_handle(cup_and_handle(False), chart.DAILY)
    assert f and f["status"] == "forming" and not f["confirmed"]


def test_bull_flag_breakout_and_forming():
    p = chart.bull_flag(bull_flag(True), chart.DAILY)
    assert p and p["status"] == "breakout" and p["confirmed"] and p["volume_dry_up"], p
    assert p["target"] > p["pivot"] > p["stop"]
    f = chart.bull_flag(bull_flag(False), chart.DAILY)
    assert f and f["status"] == "forming"


def test_bear_flag_on_mirrored_chart():
    up = bull_flag(False)
    m = up.copy()
    m["close"] = 10000 / up["close"]  # inverting keeps percentage moves: +18% becomes -15%
    m["high"], m["low"] = 10000 / up["low"], 10000 / up["high"]
    p = chart.bear_flag(m, chart.DAILY)
    assert p and p["bias"] == "bearish" and p["status"] == "forming" and p["target"] < p["pivot"] < p["stop"], p


def test_random_walk_has_few_false_patterns():
    hits = 0
    for seed in range(40):
        rng = np.random.default_rng(100 + seed)
        close = 100 * np.cumprod(1 + rng.normal(0.0003, 0.015, 400))
        d = _frame(close, rng.uniform(0.8e6, 1.2e6, 400), wiggle=0.008)
        hits += sum(1 for fn in (chart.cup_handle, chart.bull_flag) if fn(d, chart.DAILY) and fn(d, chart.DAILY)["confirmed"])
    assert hits <= 4  # confirmed breakouts should be rare on noise


def test_analyze_trend_volume_and_outlook():
    a = chart.analyze(cup_and_handle(True))
    assert a["daily"]["dir"] == "up" and a["outlook"] == "bullish"
    assert any(p["kind"] == "cup_handle" for p in a["patterns"])
    assert len(a["spark"]["d"]["c"]) == 130 and a["spark"]["w"]["c"]

    rng = np.random.default_rng(9)
    close = 300 * np.cumprod(1 + rng.normal(-0.003, 0.01, 300))
    vol = np.where(np.r_[0, np.diff(close)] < 0, 1.6e6, 0.9e6)  # heavier volume on down days
    a = chart.analyze(_frame(close, vol))
    assert a["daily"]["dir"] == "down" and a["weekly"]["dir"] == "down"
    assert a["volume"]["state"] == "distribution" and a["outlook"] == "bearish"


def test_weekly_resample_scales_partial_week():
    d = _frame(np.linspace(100, 110, 23), np.full(23, 1e6))  # 4 full weeks + Mon-Wed
    d = d.iloc[:-2]
    w = chart.to_weekly(d)
    assert abs(w["volume"].iloc[-1] - 5e6) < 1  # 3 days scaled to a full week
