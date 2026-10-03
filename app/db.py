"""SQLite persistence.

Per-user: users, sessions, user_settings, scans (+ scan_results), sweeps.
Shared market-data caches: instruments, candles, fundamentals, news.
Secret settings are encrypted at rest (see auth.encrypt / decrypt).
"""
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
SECRET_KEYS = {"api_secret", "access_token", "marketaux_key"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
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
CREATE TABLE IF NOT EXISTS sweeps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 0,
    run_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 0,
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


def _column_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r["name"] == col for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # upgrades for databases created by the single-user version
        for table in ("scans", "sweeps"):
            if not _column_exists(conn, table, "user_id"):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------- users
def create_user(email: str, password_hash: str) -> int:
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO users(email, password_hash, created_at) VALUES (?,?,?)",
                           (email.lower().strip(), password_hash, now_iso()))
        return int(cur.lastrowid)


def get_user(user_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT id, email, created_at FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_email(email: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT id, email, password_hash, created_at FROM users WHERE email=?",
                           (email.lower().strip(),)).fetchone()
    return dict(row) if row else None


def list_user_ids() -> list[int]:
    with get_conn() as conn:
        return [int(r["id"]) for r in conn.execute("SELECT id FROM users").fetchall()]


def user_count() -> int:
    with get_conn() as conn:
        return int(conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])


def create_session(token: str, user_id: int, expires_at: str) -> None:
    with get_conn() as conn:
        conn.execute("INSERT INTO sessions(token, user_id, expires_at) VALUES (?,?,?)", (token, user_id, expires_at))


def get_session(token: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT token, user_id, expires_at FROM sessions WHERE token=?", (token,)).fetchone()
    return dict(row) if row else None


def delete_session(token: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))


# ---------------------------------------------------------------- settings
def get_settings(user_id: int) -> dict[str, str]:
    from .auth import decrypt  # local import: auth imports db

    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM user_settings WHERE user_id=?", (user_id,)).fetchall()
    out = dict(DEFAULT_SETTINGS)
    for r in rows:
        out[r["key"]] = decrypt(r["value"]) if r["key"] in SECRET_KEYS else r["value"]
    return out


def set_settings(user_id: int, values: dict[str, str]) -> None:
    from .auth import encrypt

    payload = []
    for k, v in values.items():
        v = "" if v is None else str(v)
        payload.append((user_id, k, encrypt(v) if k in SECRET_KEYS and v else v))
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO user_settings(user_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
            payload,
        )


def legacy_settings() -> dict[str, str]:
    """Settings left by the single-user version, used to seed the first account."""
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def adopt_legacy_data(user_id: int) -> None:
    """Assigns pre-multi-user scans/sweeps (user_id = 0) and settings to `user_id`."""
    legacy = legacy_settings()
    if legacy:
        set_settings(user_id, {k: v for k, v in legacy.items() if k in DEFAULT_SETTINGS})
    with get_conn() as conn:
        conn.execute("UPDATE scans SET user_id=? WHERE user_id=0", (user_id,))
        conn.execute("UPDATE sweeps SET user_id=? WHERE user_id=0", (user_id,))
        conn.execute("DELETE FROM settings")


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
            for i in range(0, len(keys), 500):  # SQLite parameter limit is 999
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


def save_sweep(user_id: int, data: dict) -> int:
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO sweeps(user_id, run_at, data) VALUES (?,?,?)",
                           (user_id, data.get("run_at") or now_iso(), json.dumps(data)))
        return int(cur.lastrowid)


def latest_sweep(user_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT data FROM sweeps WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
    return json.loads(row["data"]) if row else None


# ------------------------------------------------------------------- scans
def create_scan(user_id: int, params: dict) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO scans(user_id, run_at, status, message, params) VALUES (?,?,?,?,?)",
            (user_id, now_iso(), "running", "", json.dumps(params)),
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
        "id": r["id"], "user_id": r["user_id"], "run_at": r["run_at"], "status": r["status"], "message": r["message"],
        "regime": json.loads(r["regime"] or "{}"), "params": json.loads(r["params"] or "{}"),
        "stats": json.loads(r["stats"] or "{}"),
    }


def list_scans(user_id: int, limit: int = 30) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM scans WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
    return [_scan_row(r) for r in rows]


def get_scan(user_id: int, scan_id: int | None = None) -> dict | None:
    with get_conn() as conn:
        if scan_id is None:
            row = conn.execute("SELECT * FROM scans WHERE user_id=? AND status='done' ORDER BY id DESC LIMIT 1",
                               (user_id,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM scans WHERE id=? AND user_id=?", (scan_id, user_id)).fetchone()
        if row is None:
            return None
        scan = _scan_row(row)
        res = conn.execute("SELECT data FROM scan_results WHERE scan_id=? ORDER BY rank", (scan["id"],)).fetchall()
    scan["results"] = [json.loads(r["data"]) for r in res]
    return scan
