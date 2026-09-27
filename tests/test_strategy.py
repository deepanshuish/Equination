"""Strategy, backtest and API tests on synthetic price data (no network)."""
import os
import tempfile

import numpy as np
import pandas as pd
import pytest

os.environ["EQUINATION_DATA_DIR"] = tempfile.mkdtemp()

from app import db  # noqa: E402
from app.backtest import run_backtest  # noqa: E402
from app.strategy import StrategyParams, build_picks, compute_features, market_regime, score_features  # noqa: E402


def make_series(n: int, drift: float, vol: float, seed: int, start: float = 100.0) -> pd.Series:
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, n)
    return pd.Series(start * np.cumprod(1 + rets))


def make_candles(spec: dict[str, tuple[float, float]], n: int = 400) -> pd.DataFrame:
    dates = pd.bdate_range("2023-01-02", periods=n)
    frames = []
    for i, (key, (drift, vol)) in enumerate(spec.items()):
        close = make_series(n, drift, vol, seed=i)
        frames.append(pd.DataFrame({
            "instrument_key": key, "date": dates, "open": close.values, "high": close.values * 1.01,
            "low": close.values * 0.99, "close": close.values, "volume": 2_000_000.0,
        }))
    return pd.concat(frames, ignore_index=True)


SPEC = {
    "NSE_EQ|STRONG": (0.0015, 0.012),   # smooth up-trend -> should rank first
    "NSE_EQ|WILD": (0.0015, 0.045),     # same drift, too volatile -> filtered
    "NSE_EQ|FLAT": (0.0000, 0.010),     # no momentum
    "NSE_EQ|DOWN": (-0.0015, 0.012),    # falling -> below 200 DMA
    "NSE_EQ|CHEAP": (0.0015, 0.010),    # good trend but price too low (start=100 * ... handled below)
}


@pytest.fixture
def candles() -> pd.DataFrame:
    df = make_candles(SPEC)
    df.loc[df["instrument_key"] == "NSE_EQ|CHEAP", ["open", "high", "low", "close"]] *= 0.1
    return df


@pytest.fixture
def instruments() -> pd.DataFrame:
    return pd.DataFrame([{"instrument_key": k, "symbol": k.split("|")[1], "name": k} for k in SPEC])


def test_scoring_and_filters(candles, instruments):
    p = StrategyParams(top_n=3, capital=1_000_000, min_price=50)
    scored = score_features(compute_features(candles), p)
    excl = scored.set_index(scored.index)["excluded"]
    assert scored.loc["NSE_EQ|STRONG", "eligible"]
    assert "too volatile" in excl["NSE_EQ|WILD"]
    assert "below 200 DMA" in excl["NSE_EQ|DOWN"]
    assert "price < 50" in excl["NSE_EQ|CHEAP"]
    assert scored[scored["eligible"]].index[0] == "NSE_EQ|STRONG"
    assert scored.loc["NSE_EQ|CHEAP", "rank"] == 0  # ineligible names never get a rank

    regime = market_regime(candles[candles["instrument_key"] == "NSE_EQ|STRONG"].set_index("date")["close"])
    assert regime.state == "RISK_ON" and regime.allocation == 1.0
    picks = build_picks(scored, instruments, p, regime)
    assert picks[0]["symbol"] == "STRONG" and picks[0]["rank"] == 1
    top = picks[0]
    assert 0 < top["stop_loss"] < top["close"]
    assert top["quantity"] * (top["close"] - top["stop_loss"]) <= p.capital * p.risk_per_trade_pct / 100 + top["close"]
    assert top["position_value"] <= p.capital / p.top_n + top["close"]


def test_regime_off_gives_zero_allocation(candles, instruments):
    down = candles[candles["instrument_key"] == "NSE_EQ|DOWN"].set_index("date")["close"]
    regime = market_regime(down)
    assert regime.state == "RISK_OFF" and regime.allocation == 0.0
    p = StrategyParams(top_n=3)
    picks = build_picks(score_features(compute_features(candles), p), instruments, p, regime)
    assert all(pk["quantity"] == 0 for pk in picks)


def test_backtest_runs(candles):
    nifty = candles[candles["instrument_key"] == "NSE_EQ|STRONG"].set_index("date")["close"]
    res = run_backtest(candles, nifty, StrategyParams(top_n=2))
    assert "error" not in res, res
    assert res["strategy"]["months"] >= 6
    assert len(res["curve"]) == res["strategy"]["months"]
    assert -100 <= res["strategy"]["max_drawdown_pct"] <= 0


def test_db_roundtrip(candles):
    db.init_db()
    db.set_settings({"top_n": "7", "api_secret": "x"})
    assert db.get_settings()["top_n"] == "7"
    one = candles[candles["instrument_key"] == "NSE_EQ|STRONG"].copy()
    one["date"] = one["date"].dt.strftime("%Y-%m-%d")
    assert db.upsert_candles("NSE_EQ|STRONG", one[["date", "open", "high", "low", "close", "volume"]]) == len(one)
    assert db.last_candle_dates()["NSE_EQ|STRONG"] == one["date"].max()
    scan_id = db.create_scan({"top_n": 7})
    db.finish_scan(scan_id, "done", "ok", {"state": "RISK_ON"}, {"universe": 1},
                   [{"rank": 1, "instrument_key": "NSE_EQ|STRONG", "symbol": "STRONG"}])
    latest = db.get_scan()
    assert latest["id"] == scan_id and latest["results"][0]["symbol"] == "STRONG"


def test_api_smoke():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        assert c.get("/").status_code == 200
        s = c.post("/api/settings", json={"values": {"api_key": "k", "api_secret": "s", "top_n": "5"}}).json()
        assert s["has_api_secret"] and "api_secret" not in s and s["top_n"] == "5"
        assert c.post("/api/settings", json={"values": {"schedule_time": "25:00"}}).status_code == 400
        r = c.get("/login", follow_redirects=False)
        assert r.status_code in (302, 307) and "client_id=k" in r.headers["location"]
        assert c.get("/api/scan/status").json()["running"] is False
        assert c.get("/api/status").json()["scan"]["phase"] == "idle"
