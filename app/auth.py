"""Accounts, sessions and secret encryption.

* Passwords: scrypt (stdlib) with a per-user salt.
* Sessions: random token stored server-side, sent as an HttpOnly cookie.
* Secrets (Upstox secret/token, Marketaux key): encrypted at rest with a key
  derived from EQUINATION_SECRET, or from a key file generated in the data
  directory on first run. The cipher is stdlib-only: an HMAC-SHA256 keystream
  in counter mode (encrypt) plus an HMAC-SHA256 tag over nonce+ciphertext
  (authenticate), i.e. encrypt-then-MAC with independent derived keys.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from . import db
from .config import DATA_DIR, PUBLIC_URL

SESSION_COOKIE = "eq_session"
SESSION_DAYS = 30
SECRET_SETTINGS = {"api_secret", "access_token", "marketaux_key"}


# ------------------------------------------------------------- encryption
def _master_secret() -> bytes:
    secret = os.environ.get("EQUINATION_SECRET", "")
    if not secret:
        keyfile = DATA_DIR / "secret.key"
        if not keyfile.exists():
            keyfile.write_text(secrets.token_urlsafe(48))
            try:
                keyfile.chmod(0o600)
            except OSError:
                pass
        secret = keyfile.read_text().strip()
    return secret.encode()


def _keys() -> tuple[bytes, bytes]:
    master = _master_secret()
    return (hmac.new(master, b"equination-enc", hashlib.sha256).digest(),
            hmac.new(master, b"equination-mac", hashlib.sha256).digest())


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def encrypt(value: str) -> str:
    if not value:
        return ""
    enc_key, mac_key = _keys()
    nonce = secrets.token_bytes(16)
    data = value.encode()
    ct = bytes(a ^ b for a, b in zip(data, _keystream(enc_key, nonce, len(data))))
    tag = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    return "enc:" + base64.urlsafe_b64encode(nonce + ct + tag).decode()


def decrypt(value: str) -> str:
    if not value or not value.startswith("enc:"):
        return value or ""
    try:
        blob = base64.urlsafe_b64decode(value[4:].encode())
        nonce, ct, tag = blob[:16], blob[16:-32], blob[-32:]
        enc_key, mac_key = _keys()
        if not hmac.compare_digest(hmac.new(mac_key, nonce + ct, hashlib.sha256).digest(), tag):
            return ""
        return bytes(a ^ b for a, b in zip(ct, _keystream(enc_key, nonce, len(ct)))).decode()
    except (ValueError, UnicodeDecodeError):
        return ""


# --------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_b64, digest_b64 = stored.split("$")
        salt, digest = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
    except ValueError:
        return False
    candidate = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(candidate, digest)


# ---------------------------------------------------------------- sessions
def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
    db.create_session(token, user_id, expires)
    return token


def cookie_kwargs() -> dict:
    return {"httponly": True, "samesite": "lax", "secure": PUBLIC_URL.startswith("https://"),
            "max_age": SESSION_DAYS * 86400, "path": "/"}


def user_from_request(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    sess = db.get_session(token)
    if not sess:
        return None
    if sess["expires_at"] < datetime.now(timezone.utc).isoformat(timespec="seconds"):
        db.delete_session(token)
        return None
    return db.get_user(sess["user_id"])


def require_user(request: Request) -> dict:
    user = user_from_request(request)
    if not user:
        raise HTTPException(401, "Please log in")
    return user


def signup_allowed() -> bool:
    return os.environ.get("EQUINATION_ALLOW_SIGNUP", "1") == "1"


def invite_ok(code: str | None) -> bool:
    required = os.environ.get("EQUINATION_INVITE_CODE", "")
    return not required or hmac.compare_digest((code or "").strip(), required)
