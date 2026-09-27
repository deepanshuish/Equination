"""Thin REST client for the Upstox v2/v3 APIs.

Only the endpoints Equination needs: OAuth login, the NSE instrument master and
daily historical candles. No orders are ever placed.
"""
from __future__ import annotations

import gzip
import json
import threading
import time
from datetime import date, timedelta
from urllib.parse import urlencode

import httpx
import pandas as pd

from .config import REQUESTS_PER_SECOND, UPSTOX_API_BASE, UPSTOX_INSTRUMENTS_URL


class UpstoxError(Exception):
    pass


class AuthError(UpstoxError):
    """Token missing, expired or rejected - the user has to log in again."""


class _RateLimiter:
    def __init__(self, rps: float):
        self.interval = 1.0 / max(rps, 0.05)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self.interval


_limiter = _RateLimiter(REQUESTS_PER_SECOND)


def auth_dialog_url(api_key: str, redirect_uri: str) -> str:
    q = urlencode({"response_type": "code", "client_id": api_key, "redirect_uri": redirect_uri})
    return f"{UPSTOX_API_BASE}/v2/login/authorization/dialog?{q}"


def exchange_code(api_key: str, api_secret: str, redirect_uri: str, code: str) -> str:
    resp = httpx.post(
        f"{UPSTOX_API_BASE}/v2/login/authorization/token",
        data={
            "code": code,
            "client_id": api_key,
            "client_secret": api_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        headers={"Accept": "application/json", "Api-Version": "2.0"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise AuthError(f"Token exchange failed ({resp.status_code}): {resp.text[:300]}")
    token = resp.json().get("access_token")
    if not token:
        raise AuthError(f"No access_token in response: {resp.text[:300]}")
    return token


def _headers(token: str | None) -> dict[str, str]:
    h = {"Accept": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def verify_token(token: str) -> dict:
    """Calls /v2/user/profile; raises AuthError when the token is not usable."""
    _limiter.wait()
    resp = httpx.get(f"{UPSTOX_API_BASE}/v2/user/profile", headers=_headers(token), timeout=30)
    if resp.status_code in (401, 403):
        raise AuthError("Access token rejected by Upstox - please log in again.")
    if resp.status_code != 200:
        raise UpstoxError(f"Profile check failed ({resp.status_code}): {resp.text[:200]}")
    return resp.json().get("data", {})


def fetch_nse_equities() -> list[dict]:
    """Downloads the NSE instrument master and returns cash-equity rows."""
    resp = httpx.get(UPSTOX_INSTRUMENTS_URL, timeout=120, follow_redirects=True)
    if resp.status_code != 200:
        raise UpstoxError(f"Instrument master download failed ({resp.status_code})")
    raw = resp.content
    try:
        raw = gzip.decompress(raw)
    except (OSError, EOFError):
        pass  # some proxies transparently un-gzip
    rows = json.loads(raw)
    out = []
    for r in rows:
        if r.get("segment") != "NSE_EQ" or r.get("instrument_type") != "EQ":
            continue
        sym = r.get("trading_symbol") or r.get("tradingsymbol")
        key = r.get("instrument_key")
        if not sym or not key:
            continue
        out.append({"instrument_key": key, "symbol": sym, "name": r.get("name"), "isin": r.get("isin")})
    return out


def fetch_daily_candles(instrument_key: str, from_date: date, to_date: date, token: str | None) -> pd.DataFrame:
    """Daily OHLCV between the two dates (inclusive), oldest first.

    Tries the v3 endpoint first and falls back to v2. Upstox returns candles as
    [timestamp, open, high, low, close, volume, oi].
    """
    urls = [
        f"{UPSTOX_API_BASE}/v3/historical-candle/{instrument_key}/days/1/{to_date:%Y-%m-%d}/{from_date:%Y-%m-%d}",
        f"{UPSTOX_API_BASE}/v2/historical-candle/{instrument_key}/day/{to_date:%Y-%m-%d}/{from_date:%Y-%m-%d}",
    ]
    last_err: Exception | None = None
    for url in urls:
        _limiter.wait()
        try:
            resp = httpx.get(url, headers=_headers(token), timeout=60)
        except httpx.HTTPError as e:  # network hiccup: try the other endpoint / let caller retry
            last_err = e
            continue
        if resp.status_code in (401, 403):
            raise AuthError("Access token rejected by Upstox - please log in again.")
        if resp.status_code == 429:
            time.sleep(5)
            last_err = UpstoxError("Rate limited (429)")
            continue
        if resp.status_code != 200:
            last_err = UpstoxError(f"{resp.status_code}: {resp.text[:200]}")
            continue
        candles = (resp.json().get("data") or {}).get("candles") or []
        if not candles:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame([c[:6] for c in candles], columns=["ts", "open", "high", "low", "close", "volume"])
        df["date"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Asia/Kolkata").dt.strftime("%Y-%m-%d")
        df = df.drop(columns="ts").sort_values("date").drop_duplicates("date", keep="last")
        return df.reset_index(drop=True)
    raise UpstoxError(f"Candle fetch failed for {instrument_key}: {last_err}")


def default_from_date(days: int) -> date:
    return date.today() - timedelta(days=days)
