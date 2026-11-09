"""Marketaux parsing/scoring and per-stock sentiment in the overview (no network)."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("EQUINATION_DATA_DIR", tempfile.mkdtemp())

from app import db, sentiment as snt  # noqa: E402
from app.scanner import add_sentiment  # noqa: E402


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


def test_add_sentiment_covers_every_row_and_vetoes():
    db.init_db()
    good = snt.score_articles([_art("great quarter", 0.8, 0, "AAA.NSE"), _art("upgrade", 0.5, 1, "AAA.NSE")], "AAA", "Aaa Ltd")
    bad = snt.score_articles([_art("fraud probe", -0.8, 0, "CCC.NSE"), _art("auditor quits", -0.7, 1, "CCC.NSE")], "CCC", "Ccc Ltd")
    db.save_news("AAA", good.to_dict(), [])
    db.save_news("CCC", bad.to_dict(), [])
    rows = [
        {"symbol": "CCC", "name": "Ccc Ltd", "action": "buy", "score": 90, "reason": "setup"},
        {"symbol": "BBB", "name": "Bbb Ltd", "action": "wait", "score": 50, "reason": "dip"},
        {"symbol": "AAA", "name": "Aaa Ltd", "action": "avoid", "score": 10, "reason": "downtrend"},
    ]
    stats = add_sentiment(rows, {"marketaux_key": "", "sentiment_days": "7"})
    by = {r["symbol"]: r for r in rows}
    assert by["CCC"]["action"] == "avoid" and "negative news" in by["CCC"]["reason"]
    assert by["AAA"]["sentiment_verdict"] == "positive" and by["AAA"]["action"] == "avoid"
    assert by["BBB"]["sentiment_verdict"] == "not_checked" and by["BBB"]["action"] == "wait"
    assert stats["sentiment_scored"] == 2 and stats["sentiment_excluded"] == 1


def test_collect_batches_stats_and_respects_budget(monkeypatch):
    db.init_db()
    pairs = [(f"CL{i}", f"Cl{i} Ltd") for i in range(45)]
    batches = []

    def stats(syms, key, days=7):
        batches.append(len(syms))
        return {s: {"score": 0.2, "n": 3} for s in syms}
    monkeypatch.setattr(snt, "fetch_entity_stats", stats)
    out, meta = snt.collect(pairs, "k", budget=5)
    assert meta["method"] == "entity_stats" and batches == [20, 20, 5]
    assert all(out[s].verdict == "positive" for s, _ in pairs)

    # stats not on the plan: per-symbol news for the first `budget` stocks only
    pairs = [(f"CN{i}", f"Cn{i} Ltd") for i in range(6)]
    monkeypatch.setattr(snt, "fetch_entity_stats", lambda syms, key, days=7: None)
    monkeypatch.setattr(snt, "fetch", lambda s, n, k, d=7: snt.score_articles([_art("x", 0.5, 0, f"{s}.NSE")], s, n))
    out, meta = snt.collect(pairs, "k", budget=2)
    assert meta["method"] == "news_per_symbol"
    assert [s for s, _ in pairs if out[s] is not None] == ["CN0", "CN1"]


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
