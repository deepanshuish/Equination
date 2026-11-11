"""Live quotes: parsing Upstox's full quote and judging each plan against today's price."""
import os
import tempfile
from datetime import datetime

os.environ.setdefault("EQUINATION_DATA_DIR", tempfile.mkdtemp())

from app import live  # noqa: E402

DAY = "2026-11-11"


def q(ltp, high=None, low=None, open_=None, date=DAY):
    return {"ltp": ltp, "high": high or ltp, "low": low or ltp, "open": open_, "date": date}


def test_parse_quote():
    raw = {"instrument_token": "NSE_EQ|X", "last_price": 102.0, "net_change": 2.0, "volume": 1000,
           "ohlc": {"open": 100.5, "high": 103, "low": 99.5, "close": 102.0}, "last_trade_time": "1762847400000"}
    p = live._quote(raw)
    assert p["prev_close"] == 100.0 and p["change_pct"] == 2.0 and p["open"] == 100.5
    assert p["traded_at"].endswith("+05:30") and p["date"] == p["traded_at"][:10]


def test_buy_row_uses_todays_open_and_levels():
    row = {"action": "buy", "entry_type": "open", "entry": 100, "stop_loss": 97, "target": 105, "entry_date": DAY}
    assert live.status(row, q(101, 101.5, 99.5, 100.2))["code"] == "active"
    assert "100.20" in live.status(row, q(101, 101.5, 99.5, 100.2))["text"]
    assert live.status(row, q(98, 100.5, 96.8, 100))["code"] == "stopped"
    assert live.status(row, q(104, 105.2, 99.8, 100))["code"] == "target"
    assert live.status(row, q(96, 96.5, 95, 96.2))["code"] == "skip"  # gapped below the stop


def test_wait_rows_trigger_only_when_price_gets_there():
    above = {"action": "wait", "entry_type": "above", "entry": 110, "stop_loss": 106.5, "target": 118, "entry_date": DAY}
    assert live.status(above, q(108, 109, 107))["code"] == "pending"
    assert live.status(above, q(111, 111.5, 107, 108))["code"] == "active"
    dip = {"action": "wait", "entry_type": "dip", "entry": 95, "stop_loss": 92, "target": 101, "entry_date": DAY}
    s = live.status(dip, q(98, 99, 97))
    assert s["code"] == "pending" and "3.2%" in s["text"]
    assert live.status(dip, q(96, 98, 94.8, 97.5))["code"] == "active"


def test_later_days_ignore_intraday_range():
    row = {"action": "wait", "entry_type": "above", "entry": 110, "stop_loss": 106.5, "target": 118, "entry_date": "2026-11-10"}
    assert live.status(row, q(108, 112, 107))["code"] == "pending"  # today's high isn't the buy day


def test_avoid_row_warns_below_stop():
    row = {"action": "avoid", "entry": 50, "stop_loss": 48, "target": 56, "entry_type": "hold"}
    assert live.status(row, q(47.5))["code"] == "below_stop"
    assert live.status(row, q(50))["code"] == "info"


def test_market_hours():
    assert live.market_open(datetime(2026, 11, 11, 12, 0, tzinfo=live.IST))
    assert not live.market_open(datetime(2026, 11, 11, 15, 30, tzinfo=live.IST))
    assert not live.market_open(datetime(2026, 11, 14, 11, 0, tzinfo=live.IST))  # Saturday


def test_buy_plan_for_another_day():
    row = {"action": "buy", "entry_type": "open", "entry": 100, "stop_loss": 97, "target": 105, "entry_date": "2026-11-12"}
    s = live.status(row, q(101))
    assert s["code"] == "pending" and "Thu 12 Nov" in s["text"]
    row["entry_date"] = "2026-11-09"
    assert live.status(row, q(96))["text"].endswith("below the stop-loss")
