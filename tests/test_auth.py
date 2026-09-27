"""Password gate (EQUINATION_PASSWORD) on the HTTP layer."""
import base64
import importlib
import os
import tempfile

os.environ["EQUINATION_DATA_DIR"] = tempfile.mkdtemp()


def _client_with_password(monkeypatch, password: str):
    from fastapi.testclient import TestClient
    from app import config, main

    monkeypatch.setattr(config, "PASSWORD", password)
    importlib.reload(main)
    return TestClient(main.app)


def test_password_gate(monkeypatch):
    with _client_with_password(monkeypatch, "s3cret") as c:
        assert c.get("/").status_code == 401
        assert c.get("/api/settings").status_code == 401
        assert c.get("/static/app.js").status_code == 200  # static assets are harmless
        bad = "Basic " + base64.b64encode(b"me:wrong").decode()
        assert c.get("/", headers={"Authorization": bad}).status_code == 401
        good = "Basic " + base64.b64encode(b"me:s3cret").decode()
        assert c.get("/", headers={"Authorization": good}).status_code == 200
        assert c.get("/api/settings", headers={"Authorization": good}).status_code == 200


def test_no_password_means_open(monkeypatch):
    with _client_with_password(monkeypatch, "") as c:
        assert c.get("/api/settings").status_code == 200
