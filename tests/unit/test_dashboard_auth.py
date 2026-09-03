"""Dashboard session authentication must fail closed.

The login route already refuses to mint sessions while DASHBOARD_PASSWORD is
unset, but session *verification* signs with the raw password string: an empty
key makes the HMAC publicly computable, so a forged cookie authenticated every
/api/dashboard route on a deployment that never configured a password.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import time
from pathlib import Path
from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import kiro.dashboard as dashboard_module


def _forged_cookie(password: str) -> str:
    """Recompute the session token exactly as _session_token does."""
    expires_at = int(time.time()) + 3600
    payload = str(expires_at)
    signature = hmac.new(password.encode(), payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


@pytest.fixture
def dashboard_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path / "dashboard"))
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    importlib.reload(dashboard_module)
    dashboard_module.initialize_dashboard_store()
    app = FastAPI()
    app.include_router(dashboard_module.router)
    yield app


class TestUnconfiguredPasswordFailsClosed:
    def test_forged_empty_key_cookie_is_rejected(self, dashboard_app: FastAPI) -> None:
        client = TestClient(dashboard_app)
        client.cookies.set(dashboard_module._COOKIE, _forged_cookie(""))
        response = client.get("/api/dashboard/keys")
        assert response.status_code == 401

    def test_login_still_refuses_without_password(self, dashboard_app: FastAPI) -> None:
        client = TestClient(dashboard_app)
        response = client.post("/api/dashboard/login", json={"password": ""})
        assert response.status_code == 503


class TestConfiguredPasswordStillWorks:
    def test_valid_session_authenticates(self, dashboard_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DASHBOARD_PASSWORD", "dashboard-password")
        client = TestClient(dashboard_app)
        login = client.post("/api/dashboard/login", json={"password": "dashboard-password"})
        assert login.status_code == 200
        response = client.get("/api/dashboard/keys")
        assert response.status_code == 200

    def test_cookie_signed_with_wrong_key_is_rejected(
        self, dashboard_app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DASHBOARD_PASSWORD", "dashboard-password")
        client = TestClient(dashboard_app)
        client.cookies.set(dashboard_module._COOKIE, _forged_cookie("guess"))
        response = client.get("/api/dashboard/keys")
        assert response.status_code == 401
