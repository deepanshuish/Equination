"""Accounts, sessions, per-user isolation and secret encryption."""
import os
import tempfile

os.environ.setdefault("EQUINATION_DATA_DIR", tempfile.mkdtemp())

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, db  # noqa: E402
from app.main import app  # noqa: E402


def _client():
    return TestClient(app)


def test_encrypt_roundtrip_and_tamper():
    e = auth.encrypt("super-secret")
    assert e.startswith("enc:") and auth.decrypt(e) == "super-secret"
    assert auth.decrypt(e[:-4] + "AAAA") == ""  # tag mismatch -> empty, never garbage
    assert auth.decrypt("legacy-plain") == "legacy-plain"


def test_signup_login_isolation():
    with _client() as c:
        # unauthenticated: pages redirect, API refuses
        assert c.get("/app", follow_redirects=False).status_code == 302
        assert c.get("/api/settings").status_code == 401
        assert c.get("/").status_code == 200 and c.get("/signup").status_code == 200

        r = c.post("/api/auth/signup", data={"email": "a@example.com", "password": "password1"})
        assert r.status_code == 200 and auth.SESSION_COOKIE in c.cookies
        assert c.post("/api/auth/signup", data={"email": "a@example.com", "password": "password1"}).status_code == 409
        assert c.post("/api/auth/signup", data={"email": "bad", "password": "password1"}).status_code == 400

        s = c.post("/api/settings", json={"values": {"api_key": "k1", "api_secret": "s1", "top_n": "7"}}).json()
        assert s["has_api_secret"] and "api_secret" not in s and s["top_n"] == "7"
        uid_a = c.get("/api/me").json()["id"]
        # stored encrypted, read back decrypted
        with db.get_conn() as conn:
            raw = conn.execute("SELECT value FROM user_settings WHERE user_id=? AND key='api_secret'", (uid_a,)).fetchone()["value"]
        assert raw.startswith("enc:") and db.get_settings(uid_a)["api_secret"] == "s1"
        assert c.get("/upstox/login", follow_redirects=False).status_code in (302, 307)

        c.get("/logout", follow_redirects=False)
        assert c.get("/api/settings").status_code == 401

    with _client() as c2:
        c2.post("/api/auth/signup", data={"email": "b@example.com", "password": "password2"})
        s2 = c2.get("/api/settings").json()
        assert s2["api_key"] == "" and s2["top_n"] == "10"  # user B does not see user A's settings
        assert c2.get("/api/scan/latest").json()["status"] == "none"
        assert c2.post("/api/auth/login", data={"email": "a@example.com", "password": "wrong"}).status_code == 401

    with _client() as c3:
        assert c3.post("/api/auth/login", data={"email": "A@example.com", "password": "password1"}).status_code == 200
        assert c3.get("/api/settings").json()["api_key"] == "k1"


def test_invite_code(monkeypatch):
    monkeypatch.setenv("EQUINATION_INVITE_CODE", "beta42")
    with _client() as c:
        assert c.get("/api/auth/config").json()["invite_required"] is True
        assert c.post("/api/auth/signup", data={"email": "c@example.com", "password": "password3"}).status_code == 403
        assert c.post("/api/auth/signup", data={"email": "c@example.com", "password": "password3", "invite": "beta42"}).status_code == 200
