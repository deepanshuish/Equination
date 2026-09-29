"""SQLite persistence: settings, instrument master, daily candles, scan history."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Iterator

import pandas as pd

from .config import DB_PATH, DEFAULT_SETTINGS

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instruments (
    instrument_key TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    name TEXT,
    isin TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_instruments_symbol ON instruments(symbol);
CREATE TABLE IF NOT EXISTS candles (
    instrument_key TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (instrument_key, date)
);
CREATE TABLE IF NOT EXISTS fundamentals (
    isin TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    data TEXT NOT NULL,
    raw TEXT
);
CREATE TABLE IF NOT EXISTS news (
    symbol TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    data TEXT NOT NULL,
    raw TEXT
);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT,
    regime TEXT,
    params TEXT,
    stats TEXT
);
CREATE TABLE IF NOT EXISTS scan_results (
    scan_id INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    instrument_key TEXT NOT NULL,
    symbol TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (scan_id, rank)
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- settings
def get_settings() -> dict[str, str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    out = dict(DEFAULT_SETTINGS)
    out.update({r["key"]: r["value"] for r in rows})
    return out


def set_settings(values: dict[str, str]) -> None:
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(k, "" if v is None else str(v)) for k, v in values.items()],
        )


# ------------------------------------------------------------- instruments
def upsert_instruments(rows: Iterable[dict]) -> int:
    ts = now_iso()
    payload = [(r["instrument_key"], r["symbol"], r.get("name"), r.get("isin"), ts) for r in rows]
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO instruments(instrument_key, symbol, name, isin, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(instrument_key) DO UPDATE SET symbol=excluded.symbol, name=excluded.name, "
            "isin=excluded.isin, updated_at=excluded.updated_at",
            payload,
        )
    return len(payload)


def get_instruments() -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql_query("SELECT instrument_key, symbol, name, isin, updated_at FROM instruments", conn)


# ----------------------------------------------------------------- candles
def upsert_candles(instrument_key: str, df: pd.DataFrame) -> int:
    """df columns: date (YYYY-MM-DD), open, high, low, close, volume."""
    if df.empty:
        return 0
    rows = [
        (instrument_key, str(r.date), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume))
        for r in df.itertuples(index=False)
    ]
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO candles(instrument_key, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(instrument_key, date) DO UPDATE SET open=excluded.open, high=excluded.high, "
            "low=excluded.low, close=excluded.close, volume=excluded.volume",
            rows,
        )
    return len(rows)


def last_candle_dates() -> dict[str, str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT instrument_key, MAX(date) AS d FROM candles GROUP BY instrument_key").fetchall()
    return {r["instrument_key"]: r["d"] for r in rows}


def load_candles(keys: list[str] | None = None) -> pd.DataFrame:
    """Long-format frame with columns instrument_key, date, open, high, low, close, volume."""
    with get_conn() as conn:
        if keys is None:
            df = pd.read_sql_query("SELECT * FROM candles ORDER BY instrument_key, date", conn)
        else:
            frames = []
            # SQLite parameter limit is 999; chunk the IN clause.
            for i in range(0, len(keys), 500):
                chunk = keys[i : i + 500]
                q = f"SELECT * FROM candles WHERE instrument_key IN ({','.join('?' * len(chunk))}) ORDER BY instrument_key, date"
                frames.append(pd.read_sql_query(q, conn, params=chunk))
            df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
                columns=["instrument_key", "date", "open", "high", "low", "close", "volume"]
            )
    df["date"] = pd.to_datetime(df["date"])
    return df


def candle_counts() -> dict[str, int]:
    with get_conn() as conn:
        rows = conn.execute("SELECT instrument_key, COUNT(*) AS n FROM candles GROUP BY instrument_key").fetchall()
    return {r["instrument_key"]: r["n"] for r in rows}


# ------------------------------------------------------------ fundamentals
def save_fundamentals(isin: str, data: dict, raw: dict | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO fundamentals(isin, fetched_at, data, raw) VALUES (?,?,?,?) "
            "ON CONFLICT(isin) DO UPDATE SET fetched_at=excluded.fetched_at, data=excluded.data, raw=excluded.raw",
            (isin, data.get("fetched_at") or now_iso(), json.dumps(data), json.dumps(raw) if raw is not None else None),
        )


def load_fundamentals(isin: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT isin, fetched_at, data, raw FROM fundamentals WHERE isin=?", (isin,)).fetchone()
    return dict(row) if row else None


# -------------------------------------------------------------------- news
def save_news(symbol: str, data: dict, raw) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO news(symbol, fetched_at, data, raw) VALUES (?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET fetched_at=excluded.fetched_at, data=excluded.data, raw=excluded.raw",
            (symbol.upper(), data.get("fetched_at") or now_iso(), json.dumps(data), json.dumps(raw) if raw is not None else None),
        )


def load_news(symbol: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT symbol, fetched_at, data, raw FROM news WHERE symbol=?", (symbol.upper(),)).fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------------- scans
def create_scan(params: dict) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO scans(run_at, status, message, params) VALUES (?,?,?,?)",
            (now_iso(), "running", "", json.dumps(params)),
        )
        return int(cur.lastrowid)


def finish_scan(scan_id: int, status: str, message: str, regime: dict | None, stats: dict | None,
                results: list[dict]) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE scans SET status=?, message=?, regime=?, stats=? WHERE id=?",
            (status, message, json.dumps(regime or {}), json.dumps(stats or {}), scan_id),
        )
        conn.execute("DELETE FROM scan_results WHERE scan_id=?", (scan_id,))
        conn.executemany(
            "INSERT INTO scan_results(scan_id, rank, instrument_key, symbol, data) VALUES (?,?,?,?,?)",
            [(scan_id, i, r["instrument_key"], r["symbol"], json.dumps(r)) for i, r in enumerate(results)],
        )


def _scan_row(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "run_at": r["run_at"],
        "status": r["status"],
        "message": r["message"],
        "regime": json.loads(r["regime"] or "{}"),
        "params": json.loads(r["params"] or "{}"),
        "stats": json.loads(r["stats"] or "{}"),
    }


def list_scans(limit: int = 30) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_scan_row(r) for r in rows]


def get_scan(scan_id: int | None = None) -> dict | None:
    with get_conn() as conn:
        if scan_id is None:
            row = conn.execute("SELECT * FROM scans WHERE status='done' ORDER BY id DESC LIMIT 1").fetchone()
        else:
            row = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        if row is None:
            return None
        scan = _scan_row(row)
        res = conn.execute("SELECT data FROM scan_results WHERE scan_id=? ORDER BY rank", (scan["id"],)).fetchall()
    scan["results"] = [json.loads(r["data"]) for r in res]
    return scan
