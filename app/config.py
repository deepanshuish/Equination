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
PORT = int(os.environ.get("EQUINATION_PORT", "8000"))

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
    "redirect_uri": f"http://{HOST}:{PORT}/callback",
    "access_token": "",
    "token_issued_at": "",
    "universe": "curated",  # curated | nse_all
    "capital": "500000",
    "risk_per_trade_pct": "1.0",
    "top_n": "10",
    "min_price": "50",
    "min_turnover_cr": "5",
    "schedule_time": "18:30",  # IST, after the daily candle is final
    "schedule_enabled": "1",
}
