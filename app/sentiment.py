"""News sentiment from Marketaux (https://www.marketaux.com/documentation).

One request per symbol returns recent articles, each with per-entity
`sentiment_score` in [-1, 1]. We average those scores with a recency decay and
the entity match score, cache the result for a few hours (the free tier is
100 requests/day), and turn it into a verdict the dashboards can use.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import pandas as pd

from . import db

API_URL = "https://api.marketaux.com/v1/news/all"
CACHE_HOURS = 12
HALF_LIFE_DAYS = 3.0
POSITIVE = 0.15
NEGATIVE = -0.15
STRONG_NEGATIVE = -0.35


class SentimentError(Exception):
    pass


class SentimentAuthError(SentimentError):
    """Bad or exhausted Marketaux key."""


@dataclass
class Sentiment:
    symbol: str
    fetched_at: str = ""
    score: float | None = None  # weighted mean in [-1, 1]
    n_articles: int = 0
    n_scored: int = 0
    positive: int = 0
    negative: int = 0
    verdict: str = "no_news"  # positive | neutral | negative | no_news | error
    headlines: list[dict] = field(default_factory=list)
    query: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ scoring
def _entity_matches(entity: dict, symbol: str, name: str) -> bool:
    es = str(entity.get("symbol") or "").upper()
    en = str(entity.get("name") or "").lower()
    sym = symbol.upper()
    if es == sym or es.startswith(sym + ".") or es.split(".")[0] == sym:
        return True
    words = [w for w in name.lower().replace("ltd", "").replace("limited", "").split() if len(w) > 2]
    return bool(words) and all(w in en for w in words[:2])


def score_articles(articles: list[dict], symbol: str, name: str = "", now: datetime | None = None) -> Sentiment:
    now = now or datetime.now(timezone.utc)
    s = Sentiment(symbol=symbol, fetched_at=now.isoformat(timespec="seconds"))
    num = den = 0.0
    for art in articles or []:
        published = pd.to_datetime(art.get("published_at"), utc=True, errors="coerce")
        age_days = max((now - published.to_pydatetime()).total_seconds() / 86400, 0) if pd.notna(published) else 7.0
        recency = 0.5 ** (age_days / HALF_LIFE_DAYS)
        ent_scores = []
        for ent in art.get("entities") or []:
            if not _entity_matches(ent, symbol, name):
                continue
            sc = ent.get("sentiment_score")
            if sc is None:
                continue
            match = float(ent.get("match_score") or 1.0)
            ent_scores.append((float(sc), match))
        s.n_articles += 1
        art_score = None
        if ent_scores:
            w = sum(m for _, m in ent_scores)
            art_score = sum(sc * m for sc, m in ent_scores) / w if w else None
        if art_score is not None:
            s.n_scored += 1
            weight = recency * (sum(m for _, m in ent_scores) / len(ent_scores))
            num += art_score * weight
            den += weight
            if art_score > 0.1:
                s.positive += 1
            elif art_score < -0.1:
                s.negative += 1
        s.headlines.append({
            "title": art.get("title"), "url": art.get("url"), "source": art.get("source"),
            "published_at": str(published.date()) if pd.notna(published) else None,
            "sentiment": round(art_score, 2) if art_score is not None else None,
        })
    if den > 0:
        s.score = round(num / den, 3)
    s.headlines.sort(key=lambda h: h.get("published_at") or "", reverse=True)
    s.headlines = s.headlines[:8]
    s.verdict = verdict(s.score, s.n_scored)
    return s


def verdict(score: float | None, n: int) -> str:
    if score is None or n == 0:
        return "no_news"
    if score >= POSITIVE:
        return "positive"
    if score <= NEGATIVE:
        return "negative"
    return "neutral"


def sentiment_points(s: Sentiment | None) -> float:
    """Maps a sentiment to 0-100 for the cumulative score (50 = unknown/neutral)."""
    if s is None or s.score is None or s.n_scored == 0:
        return 50.0
    conf = 1 - math.exp(-s.n_scored / 3.0)  # 1 article ~0.28, 5 articles ~0.81
    return round(50 + 50 * max(min(s.score, 1.0), -1.0) * conf, 1)


# ------------------------------------------------------------------ fetching
def fetch(symbol: str, name: str, api_key: str, days: int = 7) -> Sentiment:
    if not api_key:
        raise SentimentAuthError("No Marketaux API key configured.")
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")
    base = {"api_token": api_key, "language": "en", "filter_entities": "true", "published_after": since}
    attempts = [
        {"symbols": f"{symbol}.NSE"},
        {"search": _search_phrase(name or symbol), "countries": "in"},
    ]
    last_query = ""
    articles: list[dict] = []
    for extra in attempts:
        q = {**base, **extra}
        last_query = urlencode({k: v for k, v in q.items() if k != "api_token"})
        for attempt in range(2):
            try:
                resp = httpx.get(API_URL, params=q, timeout=30)
            except httpx.HTTPError as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                raise SentimentError(f"network: {e}")
            break
        if resp.status_code in (401, 402, 403):
            raise SentimentAuthError(f"Marketaux rejected the API key ({resp.status_code}): {resp.text[:150]}")
        if resp.status_code == 429:
            raise SentimentError("Marketaux rate limit / daily quota reached (429)")
        if resp.status_code != 200:
            raise SentimentError(f"Marketaux {resp.status_code}: {resp.text[:150]}")
        body = resp.json()
        if "error" in body:
            raise SentimentError(str(body["error"]))
        articles = body.get("data") or []
        if articles:
            break
    s = score_articles(articles, symbol, name)
    s.query = last_query
    db.save_news(symbol, s.to_dict(), articles)
    return s


def _search_phrase(name: str) -> str:
    stop = {"ltd", "ltd.", "limited", "india", "&", "and", "the", "co", "corp", "corporation"}
    words = [w for w in name.replace(",", " ").split() if w.lower() not in stop]
    return " ".join(words[:3]) or name


def get_cached(symbol: str, max_age_hours: float = CACHE_HOURS) -> Sentiment | None:
    row = db.load_news(symbol)
    if not row:
        return None
    if pd.Timestamp(row["fetched_at"]) < pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=max_age_hours):
        return None
    d = json.loads(row["data"])
    return Sentiment(**{k: d.get(k) for k in Sentiment.__dataclass_fields__ if k in d})


def get(symbol: str, name: str, api_key: str) -> Sentiment:
    cached = get_cached(symbol)
    if cached is not None:
        return cached
    return fetch(symbol, name, api_key)
