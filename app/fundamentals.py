"""Fundamentals and promoter (shareholding) data from Upstox's /v2/fundamentals API.

Upstox types these responses loosely, so the parsers here are deliberately
tolerant: ratios are matched by name substring and shareholding history is
accepted as a dict or a list. The raw payload is cached so the UI can show it
and the mapping can be corrected if a field name differs.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
import pandas as pd

from . import db
from .config import UPSTOX_API_BASE
from .upstox_client import AuthError, UpstoxError, _headers, _limiter

CACHE_DAYS = 7
FINANCIAL_WORDS = ("bank", "finan", "nbfc", "insur", "capital", "housing", "credit", "lending", "amc", "securities")

RATIO_ALIASES: dict[str, tuple[str, ...]] = {
    "pe": ("p/e", "pe ratio", "price to earning", "price/earning"),
    "pb": ("p/b", "pb ratio", "price to book", "price/book"),
    "roe": ("return on equity", "roe"),
    "roce": ("return on capital", "roce"),
    "debt_equity": ("debt to equity", "debt/equity", "d/e"),
    "net_margin": ("net profit margin", "net margin", "npm"),
    "op_margin": ("operating margin", "operating profit margin", "ebit margin", "opm"),
    "eps_growth": ("eps growth", "earnings growth", "profit growth", "pat growth", "net profit growth"),
    "revenue_growth": ("revenue growth", "sales growth", "net sales growth"),
    "dividend_yield": ("dividend yield",),
    "interest_coverage": ("interest coverage",),
    "current_ratio": ("current ratio",),
}


@dataclass
class Fundamentals:
    isin: str
    fetched_at: str = ""
    ratios: dict[str, float | None] = field(default_factory=dict)
    promoter_pct: float | None = None
    promoter_prev_pct: float | None = None
    promoter_change_pp: float | None = None
    promoter_periods: list[str] = field(default_factory=list)
    pledge_pct: float | None = None
    fii_pct: float | None = None
    dii_pct: float | None = None
    is_financial: bool = False
    has_ratios: bool = False
    has_holdings: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ parsing
def _num(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("%", "")
    if s in ("", "-", "NA", "N/A", "null", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        m = re.search(r"-?\d+(\.\d+)?", s)
        return float(m.group()) if m else None


def parse_key_ratios(payload) -> dict[str, float | None]:
    """Accepts the Upstox 'data' value (list of {name, company_value, ...} or a dict)."""
    out: dict[str, float | None] = {}
    items: list[tuple[str, object]] = []
    if isinstance(payload, dict):
        for k, v in payload.items():
            if isinstance(v, dict) and "company_value" in v:
                items.append((str(v.get("name", k)), v.get("company_value")))
            elif isinstance(v, list):
                for it in v:
                    if isinstance(it, dict) and "name" in it:
                        items.append((str(it["name"]), it.get("company_value", it.get("value"))))
            else:
                items.append((str(k), v))
    elif isinstance(payload, list):
        for it in payload:
            if isinstance(it, dict) and "name" in it:
                items.append((str(it["name"]), it.get("company_value", it.get("value"))))
    for name, value in items:
        lname = name.lower()
        for key, aliases in RATIO_ALIASES.items():
            if key in out and out[key] is not None:
                continue
            if any(a in lname for a in aliases):
                out[key] = _num(value)
                break
    return out


def _history_points(history) -> list[tuple[str, float]]:
    """(label, value) pairs sorted oldest -> newest, from a dict or list history."""
    pts: list[tuple[str, float]] = []
    if isinstance(history, dict):
        for k, v in history.items():
            n = _num(v.get("value", v.get("percentage")) if isinstance(v, dict) else v)
            if n is not None:
                pts.append((str(k), n))
    elif isinstance(history, list):
        for it in history:
            if isinstance(it, dict):
                label = it.get("period") or it.get("date") or it.get("quarter") or it.get("label") or it.get("name")
                n = _num(it.get("value", it.get("percentage", it.get("holding"))))
                if label is not None and n is not None:
                    pts.append((str(label), n))
    if not pts:
        return pts
    parsed = [pd.to_datetime(lbl, errors="coerce") for lbl, _ in pts]
    if all(pd.notna(p) for p in parsed):
        order = sorted(range(len(pts)), key=lambda i: parsed[i])
        pts = [pts[i] for i in order]
    return pts


def parse_share_holdings(payload) -> dict:
    """Extracts promoter / FII / DII / pledge series from the 'data' value."""
    res: dict = {"promoter": [], "pledge": [], "fii": [], "dii": []}
    entries: list[tuple[str, object]] = []
    if isinstance(payload, list):
        for it in payload:
            if isinstance(it, dict):
                entries.append((str(it.get("category") or it.get("name") or ""), it.get("history", it.get("data", it))))
    elif isinstance(payload, dict):
        for k, v in payload.items():
            entries.append((str(k), v))
    for cat, hist in entries:
        lc = cat.lower()
        pts = _history_points(hist)
        if not pts:
            continue
        if "pledg" in lc:
            res["pledge"] = pts
        elif "promoter" in lc:
            res["promoter"] = pts
        elif "fii" in lc or "foreign" in lc:
            res["fii"] = pts
        elif "dii" in lc or "domestic" in lc or "mutual" in lc:
            res["dii"] = pts
    return res


def build(isin: str, ratios_raw, holdings_raw, name: str = "", errors: list[str] | None = None) -> Fundamentals:
    f = Fundamentals(isin=isin, fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     errors=errors or [])
    f.is_financial = any(w in (name or "").lower() for w in FINANCIAL_WORDS)
    if ratios_raw is not None:
        f.ratios = parse_key_ratios(ratios_raw)
        f.has_ratios = any(v is not None for v in f.ratios.values())
    if holdings_raw is not None:
        sh = parse_share_holdings(holdings_raw)
        f.has_holdings = bool(sh["promoter"])
        if sh["promoter"]:
            pts = sh["promoter"]
            f.promoter_pct = pts[-1][1]
            f.promoter_periods = [p[0] for p in pts[-4:]]
            if len(pts) >= 2:
                # compare with the value two quarters back when available
                prev = pts[-3][1] if len(pts) >= 3 else pts[-2][1]
                f.promoter_prev_pct = prev
                f.promoter_change_pp = round(f.promoter_pct - prev, 2)
        if sh["pledge"]:
            f.pledge_pct = sh["pledge"][-1][1]
        if sh["fii"]:
            f.fii_pct = sh["fii"][-1][1]
        if sh["dii"]:
            f.dii_pct = sh["dii"][-1][1]
    return f


# ------------------------------------------------------------------ fetching
def _get(path: str, token: str):
    _limiter.wait()
    resp = httpx.get(f"{UPSTOX_API_BASE}{path}", headers=_headers(token), timeout=30)
    if resp.status_code in (401, 403):
        raise AuthError("Access token rejected by Upstox - please log in again.")
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise UpstoxError(f"{path}: {resp.status_code} {resp.text[:150]}")
    body = resp.json()
    return body.get("data", body)


def fetch(isin: str, token: str, name: str = "") -> Fundamentals:
    errors: list[str] = []
    ratios = holdings = None
    try:
        ratios = _get(f"/v2/fundamentals/{isin}/key-ratios", token)
    except AuthError:
        raise
    except (UpstoxError, ValueError) as e:
        errors.append(f"ratios: {e}")
    try:
        holdings = _get(f"/v2/fundamentals/{isin}/share-holdings", token)
    except AuthError:
        raise
    except (UpstoxError, ValueError) as e:
        errors.append(f"holdings: {e}")
    f = build(isin, ratios, holdings, name, errors)
    db.save_fundamentals(isin, f.to_dict(), {"key_ratios": ratios, "share_holdings": holdings})
    return f


def get_cached(isin: str) -> Fundamentals | None:
    row = db.load_fundamentals(isin)
    if not row:
        return None
    fetched = pd.Timestamp(row["fetched_at"])
    if fetched < pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=CACHE_DAYS):
        return None
    d = json.loads(row["data"])
    return Fundamentals(**{k: d.get(k) for k in Fundamentals.__dataclass_fields__ if k in d})


def get(isin: str, token: str, name: str = "") -> Fundamentals:
    cached = get_cached(isin)
    if cached is not None:
        return cached
    return fetch(isin, token, name)


# ------------------------------------------------------------------ scoring
@dataclass
class QualityParams:
    min_roe: float = 10.0
    max_pe: float = 80.0
    max_debt_equity: float = 1.5
    min_promoter_pct: float = 25.0  # below this is a warning, not an exclusion
    max_promoter_drop_pp: float = 1.5  # promoter selling this much (2 quarters) excludes
    max_pledge_pct: float = 15.0
    require_fundamentals: bool = False  # exclude when Upstox has no data


def assess(f: Fundamentals | None, p: QualityParams) -> dict:
    """Returns {'ok': bool, 'score': 0-100, 'flags': [...], 'hard': [...]}.

    `hard` reasons exclude the stock; `flags` are warnings shown in the UI.
    """
    hard: list[str] = []
    flags: list[str] = []
    score = 50.0
    if f is None or (not f.has_ratios and not f.has_holdings):
        if p.require_fundamentals:
            hard.append("no fundamentals data")
        else:
            flags.append("fundamentals unverified")
        return {"ok": not hard, "score": 50.0, "flags": flags, "hard": hard}

    r = f.ratios
    roe = r.get("roe")
    pe = r.get("pe")
    de = r.get("debt_equity")
    nm = r.get("net_margin")
    eg = r.get("eps_growth")
    rg = r.get("revenue_growth")

    if roe is not None:
        if roe < p.min_roe:
            hard.append(f"ROE {roe:.1f}% < {p.min_roe:g}%")
        score += min(max(roe - p.min_roe, -20), 20)
    if pe is not None:
        if pe <= 0:
            hard.append("loss-making (P/E <= 0)")
        elif pe > p.max_pe:
            hard.append(f"P/E {pe:.0f} > {p.max_pe:g}")
        elif pe < 40:
            score += 5
    if de is not None and not f.is_financial:
        if de > p.max_debt_equity:
            hard.append(f"D/E {de:.2f} > {p.max_debt_equity:g}")
        else:
            score += 5 * (1 - min(de, 1.0))
    if nm is not None:
        if nm < 0:
            hard.append("negative net margin")
        else:
            score += min(nm / 4, 5)
    if eg is not None:
        score += max(min(eg / 5, 10), -10)
        if eg < -20:
            flags.append(f"profit growth {eg:.0f}%")
    if rg is not None and rg < 0:
        flags.append(f"revenue growth {rg:.0f}%")

    if f.has_holdings and f.promoter_pct is not None:
        if 0 < f.promoter_pct < p.min_promoter_pct:
            flags.append(f"low promoter holding {f.promoter_pct:.1f}%")
        elif f.promoter_pct >= 50:
            score += 5
        if f.promoter_change_pp is not None:
            if f.promoter_change_pp <= -p.max_promoter_drop_pp:
                hard.append(f"promoters cut stake {f.promoter_change_pp:+.1f} pp")
            elif f.promoter_change_pp >= 0.5:
                score += 5
                flags.append(f"promoters adding {f.promoter_change_pp:+.1f} pp")
    if f.pledge_pct is not None and f.pledge_pct > p.max_pledge_pct:
        hard.append(f"pledged {f.pledge_pct:.0f}% > {p.max_pledge_pct:g}%")
    elif f.pledge_pct is not None and f.pledge_pct > 0:
        flags.append(f"pledged {f.pledge_pct:.0f}%")
    if not f.has_ratios:
        flags.append("ratios unavailable")
    if not f.has_holdings:
        flags.append("shareholding unavailable")

    return {"ok": not hard, "score": round(max(0.0, min(score, 100.0)), 0), "flags": flags, "hard": hard}


def cache_age_days(f: Fundamentals) -> float:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(f.fetched_at)).total_seconds() / 86400
    except ValueError:
        return float("nan")


def stale_before() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS)).isoformat(timespec="seconds")
