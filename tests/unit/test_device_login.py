"""Kiro social device-authorization login.

Three properties of this upstream broke the reference implementation's OIDC code
and are pinned here: pending arrives as HTTP 200 with a status field, timings are
milliseconds, and the poll response carries the profileArn directly.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from kiro import device_login
from kiro.device_login import (
    DeviceLoginError,
    discard_flow,
    poll_device_login,
    resolve_provider,
    start_device_login,
)

AUTHORIZATION = {
    "deviceCode": "device-code-value",
    "userCode": "WXYZ-1234",
    "verificationUri": "https://app.kiro.dev/account/device",
    "verificationUriComplete": "https://app.kiro.dev/account/device?user_code=WXYZ-1234",
    "expiresInMilliseconds": 300_000,
    "intervalInMilliseconds": 5_000,
}

APPROVED = {
    "status": "complete",
    "accessToken": "access-token-value",
    "refreshToken": "refresh-token-value-long-enough-for-validation",
    "profileArn": "arn:aws:codewhisperer:us-east-1:699475941385:profile/EHGA3GRVQMUK",
    "identityProvider": "Google",
}

PENDING = {"status": "authorization_pending", "accessToken": None, "refreshToken": None, "profileArn": None, "identityProvider": None}


class _Response:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


def _client(*responses: _Response) -> AsyncMock:
    client = AsyncMock()
    client.post = AsyncMock(side_effect=list(responses))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


@pytest.fixture(autouse=True)
def clean_flows():
    device_login._flows.clear()
    yield
    device_login._flows.clear()


def test_provider_names_are_normalized():
    assert resolve_provider("google") == "Google"
    assert resolve_provider("GitHub") == "Github"


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError):
        resolve_provider("facebook")


@pytest.mark.asyncio
async def test_start_converts_milliseconds_to_seconds():
    """Upstream reports ms; feeding those numbers into second-based logic would
    turn a 5s poll interval into 5000s."""
    with patch("kiro.device_login.httpx.AsyncClient", return_value=_client(_Response(AUTHORIZATION))):
        flow = await start_device_login("Google")

    assert flow.interval_seconds == 5
    assert 290 < flow.expires_at - time.time() <= 300
    assert flow.view()["expiresInSeconds"] <= 300


@pytest.mark.asyncio
async def test_start_returns_the_approval_link():
    with patch("kiro.device_login.httpx.AsyncClient", return_value=_client(_Response(AUTHORIZATION))):
        view = (await start_device_login("Google")).view()

    assert view["verificationUriComplete"] == AUTHORIZATION["verificationUriComplete"]
    assert view["userCode"] == "WXYZ-1234"
    assert view["status"] == "pending"


@pytest.mark.asyncio
async def test_flow_view_never_exposes_the_device_code_or_token():
    with patch("kiro.device_login.httpx.AsyncClient", return_value=_client(_Response(AUTHORIZATION))):
        flow = await start_device_login("Google")
    flow.token = {"refreshToken": "secret-refresh"}

    serialized = str(flow.view())

    assert "device-code-value" not in serialized
    assert "secret-refresh" not in serialized


@pytest.mark.asyncio
async def test_upstream_error_on_start_is_surfaced():
    with patch(
        "kiro.device_login.httpx.AsyncClient",
        return_value=_client(_Response({"message": "bad provider"}, status_code=400)),
    ):
        with pytest.raises(DeviceLoginError) as exc_info:
            await start_device_login("Google")

    assert "bad provider" in str(exc_info.value)


@pytest.mark.asyncio
async def test_pending_poll_is_not_treated_as_success():
    """Pending is HTTP 200 with a status field. Code that infers success from the
    absence of an exception stores accessToken: None."""
    with patch("kiro.device_login.httpx.AsyncClient", return_value=_client(_Response(AUTHORIZATION))):
        flow = await start_device_login("Google")

    with patch("kiro.device_login.httpx.AsyncClient", return_value=_client(_Response(PENDING))):
        polled = await poll_device_login(flow.id)

    assert polled.status == "pending"
    assert polled.token is None


@pytest.mark.asyncio
async def test_approved_poll_captures_the_refresh_token_and_profile():
    with patch("kiro.device_login.httpx.AsyncClient", return_value=_client(_Response(AUTHORIZATION))):
        flow = await start_device_login("Google")

    with patch("kiro.device_login.httpx.AsyncClient", return_value=_client(_Response(APPROVED))):
        polled = await poll_device_login(flow.id)

    assert polled.status == "approved"
    assert polled.token is not None
    assert polled.token["refreshToken"] == APPROVED["refreshToken"]
    assert polled.token["profileArn"] == APPROVED["profileArn"]


@pytest.mark.asyncio
async def test_expired_upstream_status_ends_the_flow():
    with patch("kiro.device_login.httpx.AsyncClient", return_value=_client(_Response(AUTHORIZATION))):
        flow = await start_device_login("Google")

    with patch(
        "kiro.device_login.httpx.AsyncClient",
        return_value=_client(_Response({**PENDING, "status": "expired_token"})),
    ):
        polled = await poll_device_login(flow.id)

    assert polled.status == "expired"
    assert polled.detail is not None and "expired_token" in polled.detail


@pytest.mark.asyncio
async def test_poll_past_the_deadline_expires_without_calling_upstream():
    with patch("kiro.device_login.httpx.AsyncClient", return_value=_client(_Response(AUTHORIZATION))):
        flow = await start_device_login("Google")
    flow.expires_at = time.time() - 1

    with patch("kiro.device_login.httpx.AsyncClient") as client_factory:
        polled = await poll_device_login(flow.id)

    assert polled.status == "expired"
    client_factory.assert_not_called()


@pytest.mark.asyncio
async def test_polling_an_unknown_flow_raises():
    with pytest.raises(KeyError):
        await poll_device_login("does-not-exist")


@pytest.mark.asyncio
async def test_discarding_a_flow_forgets_the_token():
    with patch("kiro.device_login.httpx.AsyncClient", return_value=_client(_Response(AUTHORIZATION))):
        flow = await start_device_login("Google")

    discard_flow(flow.id)

    with pytest.raises(KeyError):
        await poll_device_login(flow.id)


@pytest.mark.asyncio
async def test_a_resolved_flow_stops_calling_upstream():
    with patch("kiro.device_login.httpx.AsyncClient", return_value=_client(_Response(AUTHORIZATION))):
        flow = await start_device_login("Google")
    with patch("kiro.device_login.httpx.AsyncClient", return_value=_client(_Response(APPROVED))):
        await poll_device_login(flow.id)

    with patch("kiro.device_login.httpx.AsyncClient") as client_factory:
        polled = await poll_device_login(flow.id)

    assert polled.status == "approved"
    client_factory.assert_not_called()


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ACCOUNTS_CONFIG_FILE", str(tmp_path / "credentials.json"))
    module = importlib.reload(importlib.import_module("kiro.dashboard"))
    module.initialize_dashboard_store()
    module._authenticated = lambda _request: True
    return module


def _request_with_manager(tmp_path):
    from unittest.mock import MagicMock

    from kiro.account_manager import AccountManager

    manager = AccountManager(
        credentials_file=str(tmp_path / "credentials.json"),
        state_file=str(tmp_path / "state.json"),
    )
    request = MagicMock()
    request.app.state.account_manager = manager
    return request, manager


@pytest.mark.asyncio
async def test_registering_an_approved_login_adds_the_account(dashboard, tmp_path):
    with patch("kiro.device_login.httpx.AsyncClient", return_value=_client(_Response(AUTHORIZATION))):
        flow = await start_device_login("Google")
    with patch("kiro.device_login.httpx.AsyncClient", return_value=_client(_Response(APPROVED))):
        await poll_device_login(flow.id)

    request, manager = _request_with_manager(tmp_path)
    with patch.object(manager, "_initialize_account", AsyncMock(return_value=True)):
        result = await dashboard.dashboard_register_device_login(flow.id, request)

    assert result["initialized"] is True
    assert result["provider"] == "Google"

    entries = __import__("json").loads((tmp_path / "credentials.json").read_text())
    assert entries[0]["type"] == "refresh_token"
    assert entries[0]["refresh_token"] == APPROVED["refreshToken"]
    assert entries[0]["profile_arn"] == APPROVED["profileArn"]


@pytest.mark.asyncio
async def test_registering_forgets_the_flow_afterwards(dashboard, tmp_path):
    with patch("kiro.device_login.httpx.AsyncClient", return_value=_client(_Response(AUTHORIZATION))):
        flow = await start_device_login("Google")
    with patch("kiro.device_login.httpx.AsyncClient", return_value=_client(_Response(APPROVED))):
        await poll_device_login(flow.id)

    request, manager = _request_with_manager(tmp_path)
    with patch.object(manager, "_initialize_account", AsyncMock(return_value=True)):
        await dashboard.dashboard_register_device_login(flow.id, request)

    assert flow.id not in device_login._flows


@pytest.mark.asyncio
async def test_registering_a_pending_login_is_rejected(dashboard, tmp_path):
    with patch("kiro.device_login.httpx.AsyncClient", return_value=_client(_Response(AUTHORIZATION))):
        flow = await start_device_login("Google")

    request, _manager = _request_with_manager(tmp_path)
    with patch("kiro.device_login.httpx.AsyncClient", return_value=_client(_Response(PENDING))):
        with pytest.raises(Exception) as exc_info:
            await dashboard.dashboard_register_device_login(flow.id, request)

    assert getattr(exc_info.value, "status_code", None) == 409
    assert not (tmp_path / "credentials.json").exists()


@pytest.mark.asyncio
async def test_registering_an_unknown_flow_is_a_not_found(dashboard, tmp_path):
    request, _manager = _request_with_manager(tmp_path)

    with pytest.raises(Exception) as exc_info:
        await dashboard.dashboard_register_device_login("nope", request)

    assert getattr(exc_info.value, "status_code", None) == 404
