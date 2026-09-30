"""Marketaux parsing/scoring and the cumulative ranking (no network)."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("EQUINATION_DATA_DIR", tempfile.mkdtemp())

from app import db, sentiment as snt  # noqa: E402
from app.scanner import add_sentiment_and_rank  # noqa: E402


def _art(title, score, days_ago, symbol="INFY.NSE", match=1.0):
    name = {"INFY.NSE": "Infosys Ltd", "TCS.NSE": "Tata Consultancy Services"}.get(symbol, symbol.split(".")[0].title() + " Ltd")
    return {"title": title, "url": "https://x/" + title, "source": "x.com",
            "published_at": (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(),
            "entities": [{"symbol": symbol, "name": name, "sentiment_score": score, "match_score": match}]}


def test_score_articles_recency_and_verdict():
    arts = [_art("beat", 0.6, 0), _art("miss", -0.2, 6), _art("other co", -0.9, 0, symbol="TCS.NSE")]
    s = snt.score_articles(arts, "INFY", "Infosys Ltd")
    assert s.n_articles == 3 and s.n_scored == 2  # the TCS-only article is not scored
    assert s.score > 0.3 and s.verdict == "positive"
    assert s.positive == 1 and s.negative == 1
    assert s.headlines[0]["title"] in ("beat", "other co")  # newest first
    assert snt.score_articles([], "INFY").verdict == "no_news"
    neg = snt.score_articles([_art("fraud", -0.7, 1), _art("probe", -0.5, 2)], "INFY", "Infosys Ltd")
    assert neg.verdict == "negative" and neg.score <= snt.STRONG_NEGATIVE


def test_sentiment_points_confidence():
    assert snt.sentiment_points(None) == 50
    one = snt.score_articles([_art("a", 1.0, 0)], "INFY")
    five = snt.score_articles([_art(f"a{i}", 1.0, 0) for i in range(5)], "INFY")
    assert 50 < snt.sentiment_points(one) < snt.sentiment_points(five) <= 100


def test_add_sentiment_and_rank_uses_cache_and_vetoes():
    db.init_db()
    good = snt.score_articles([_art("great quarter", 0.8, 0, "AAA.NSE"), _art("upgrade", 0.5, 1, "AAA.NSE")], "AAA", "Aaa Ltd")
    bad = snt.score_articles([_art("fraud probe", -0.8, 0, "CCC.NSE"), _art("auditor quits", -0.7, 1, "CCC.NSE")], "CCC", "Ccc Ltd")
    assert good.n_scored == 2 and bad.n_scored == 2
    db.save_news("AAA", good.to_dict(), [])
    db.save_news("CCC", bad.to_dict(), [])
    picks = [
        {"quant_rank": 1, "symbol": "CCC", "name": "Ccc Ltd", "quality_score": 70},
        {"quant_rank": 2, "symbol": "BBB", "name": "Bbb Ltd", "quality_score": 50},
        {"quant_rank": 3, "symbol": "AAA", "name": "Aaa Ltd", "quality_score": 80},
    ]
    settings = {"marketaux_key": "", "sentiment_days": "7", "w_quant": "55", "w_sentiment": "30", "w_quality": "15"}
    out, stats = add_sentiment_and_rank(picks, settings, top_n=2)
    by = {p["symbol"]: p for p in out}
    assert by["CCC"]["rank"] == 1 and by["CCC"]["cum_excluded"] and by["CCC"]["cum_rank"] == 0
    assert by["AAA"]["rank"] == 0 and by["AAA"]["cum_rank"] == 2  # outside quant top 2, inside cumulative top 2
    assert by["BBB"]["cum_rank"] == 1 and by["BBB"]["sentiment_verdict"] == "no_news"
    assert by["AAA"]["cum_score"] > 0 and by["AAA"]["sentiment_points"] > 60
    assert stats["quant_picks"] == 2 and stats["cumulative_picks"] == 2 and stats["sentiment_excluded"] == 1


def test_sweep_uses_entity_stats_then_falls_back(monkeypatch):
    universe = [("SW1", "Sw1 Ltd"), ("SW2", "Sw2 Ltd"), ("SW3", "Sw3 Ltd")]

    # 1) entity stats available: one request per batch, ranked by avg with >= 2 docs
    monkeypatch.setattr(snt, "fetch_entity_stats", lambda syms, key, days=7: {
        "SW1": {"score": 0.4, "n": 5}, "SW2": {"score": 0.9, "n": 1}, "SW3": {"score": -0.5, "n": 3}})
    res = snt.sweep(universe, "k", budget=10, batch=10)
    assert res["method"] == "entity_stats" and res["requests_used"] == 1
    assert [r["symbol"] for r in res["all"]] == ["SW1", "SW3"]  # SW2 has only 1 article
    assert res["all"][0]["verdict"] == "positive" and res["all"][-1]["verdict"] == "negative"

    # 2) endpoint not on the plan (None) -> per-symbol news within budget, cache first
    monkeypatch.setattr(snt, "fetch_entity_stats", lambda syms, key, days=7: None)
    calls = []

    def fake_fetch(symbol, name, key, days=7):
        calls.append(symbol)
        s = snt.score_articles([_art("x", 0.3, 0, f"{symbol}.NSE"), _art("y", 0.5, 1, f"{symbol}.NSE")], symbol, name)
        db.save_news(symbol, s.to_dict(), [])
        return s
    monkeypatch.setattr(snt, "fetch", fake_fetch)
    db.init_db()
    res = snt.sweep(universe, "k", budget=2)
    assert res["method"] == "news_per_symbol" and res["requests_used"] == 2 and calls == ["SW1", "SW2"]
    assert res["scored"] == 2  # SW3 had no cache and no budget left
