"""Paths and default settings for Equination."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("EQUINATION_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "equination.db"
STATIC_DIR = BASE_DIR / "static"

HOST = os.environ.get("EQUINATION_HOST", "127.0.0.1")
# Hosting platforms (Railway, Render, Fly) inject PORT; honour it when set.
PORT = int(os.environ.get("EQUINATION_PORT") or os.environ.get("PORT") or "8000")

# When set, every page and API call requires HTTP Basic auth with this password
# (any username). Required if you expose the app beyond localhost - it stores
# your Upstox secret and token.
PASSWORD = os.environ.get("EQUINATION_PASSWORD", "")

# Public base URL, used to build the default OAuth redirect URI when deployed
# (e.g. https://equination.fly.dev). Falls back to the local host/port.
PUBLIC_URL = os.environ.get("EQUINATION_PUBLIC_URL", "").rstrip("/") or f"http://{HOST}:{PORT}"

UPSTOX_API_BASE = "https://api.upstox.com"
UPSTOX_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
NIFTY_KEY = "NSE_INDEX|Nifty 50"

# Upstox publishes 50 req/s, 500 req/min and 2000 req/30 min. The 30-minute
# ceiling is the binding one for a full scan, so we default to ~1.1 req/s.
REQUESTS_PER_SECOND = float(os.environ.get("EQUINATION_RPS", "1.1"))

# How much daily history to keep. The strategy needs ~260 bars; the backtest
# benefits from more.
HISTORY_DAYS = int(os.environ.get("EQUINATION_HISTORY_DAYS", str(365 * 6)))

DEFAULT_SETTINGS: dict[str, str] = {
    "api_key": "",
    "api_secret": "",
    "redirect_uri": f"{PUBLIC_URL}/callback",
    "access_token": "",
    "token_issued_at": "",
    "universe": "curated",  # curated | nse_all
    "mode": "swing",  # swing (4-day) | positional (monthly momentum)
    "hold_days": "4",
    "require_fundamentals": "0",
    "exclude_symbols": "",  # comma-separated symbols you never want suggested
    "marketaux_key": "",
    "sentiment_days": "7",
    "w_quant": "55",  # cumulative score weights, percent
    "w_sentiment": "30",
    "w_quality": "15",
    "sweep_budget": "40",  # max fresh Marketaux requests per sweep
    "sweep_in_scan": "0",  # also run the sweep at the end of every scan
    "capital": "500000",
    "risk_per_trade_pct": "1.0",
    "top_n": "10",
    "min_price": "50",
    "min_turnover_cr": "5",
    "schedule_time": "18:30",  # IST, after the daily candle is final
    "schedule_enabled": "1",
}
