"""Forward-grading of past picks against later candles."""
import os
import tempfile

import pandas as pd

os.environ.setdefault("EQUINATION_DATA_DIR", tempfile.mkdtemp())

from app import db, performance  # noqa: E402
from app.config import NIFTY_KEY  # noqa: E402


def _bars(key, rows, start="2026-01-05"):
    dates = pd.bdate_range(start, periods=len(rows))
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["date"] = dates.strftime("%Y-%m-%d"); df["volume"] = 1e6
    db.upsert_candles(key, df[["date", "open", "high", "low", "close", "volume"]])


def test_grade_stop_target_time_and_open():
    db.init_db()
    # signal day = 2026-01-05 (index 0); trades start on index 1
    _bars("NSE_EQ|TGT", [[100, 101, 99, 100], [101, 103, 100, 102], [102, 111, 101, 110]])          # hits target 110 on day 2
    _bars("NSE_EQ|STP", [[100, 101, 99, 100], [100, 101, 94, 95], [95, 96, 90, 91]])                # low <= stop 95 on day 1
    _bars("NSE_EQ|TIM", [[100, 101, 99, 100], [100, 102, 99, 101], [101, 103, 100, 102], [102, 104, 101, 103], [103, 105, 102, 104]])  # 4 days, no stop/target
    _bars("NSE_EQ|OPN", [[100, 101, 99, 100], [100, 102, 99, 101]])                                  # only 1 day after signal
    _bars(NIFTY_KEY, [[1000, 1010, 990, 1000], [1000, 1010, 990, 1005], [1005, 1020, 1000, 1015], [1015, 1020, 1000, 1010], [1010, 1030, 1005, 1025]])
    sid = db.create_scan(7, {"mode": "swing", "hold_days": 4, "top_n": 10})
    picks = [
        {"rank": 1, "cum_rank": 1, "instrument_key": "NSE_EQ|TGT", "symbol": "TGT", "entry": 100, "stop_loss": 95, "target": 110, "sentiment_verdict": "positive"},
        {"rank": 2, "cum_rank": 0, "instrument_key": "NSE_EQ|STP", "symbol": "STP", "entry": 100, "stop_loss": 95, "target": 110, "sentiment_verdict": "no_news"},
        {"rank": 3, "cum_rank": 2, "instrument_key": "NSE_EQ|TIM", "symbol": "TIM", "entry": 100, "stop_loss": 95, "target": 120, "sentiment_verdict": "neutral"},
        {"rank": 4, "cum_rank": 3, "instrument_key": "NSE_EQ|OPN", "symbol": "OPN", "entry": 100, "stop_loss": 95, "target": 120, "sentiment_verdict": "no_news"},
        {"rank": 0, "cum_rank": 0, "instrument_key": "NSE_EQ|TGT", "symbol": "IGNORED", "entry": 1, "stop_loss": 1, "target": 1},
    ]
    db.finish_scan(sid, "done", "ok", {"state": "RISK_ON"}, {"mode": "swing", "data_as_of": "2026-01-05"}, picks)

    rep = performance.grade_all(7)
    assert performance.grade_all(8)["outcomes"] == []  # other users see nothing
    by = {o["symbol"]: o for o in rep["outcomes"]}
    assert set(by) == {"TGT", "STP", "TIM", "OPN"}
    assert by["TGT"]["exit_reason"] == "target" and by["TGT"]["exit"] == 110 and by["TGT"]["entry"] == 101 and by["TGT"]["days_held"] == 2
    assert by["STP"]["exit_reason"] == "stop" and by["STP"]["exit"] == 95 and by["STP"]["ret_pct"] < 0
    assert by["TIM"]["exit_reason"] == "time" and by["TIM"]["exit"] == 104 and by["TIM"]["days_held"] == 4
    assert by["OPN"]["status"] == "open" and by["OPN"]["exit_reason"] == "open"
    s = rep["stats"]
    assert s["trades"] == 3 and s["open"] == 1 and s["win_rate_pct"] == round(2 / 3 * 100, 1)
    assert rep["by_list"]["cumulative"]["trades"] == 2  # TGT and TIM (OPN still open)
    assert rep["by_sentiment"]["positive"]["trades"] == 1
    assert len(rep["curve"]) == 3 and by["TGT"]["nifty_ret_pct"] is not None
