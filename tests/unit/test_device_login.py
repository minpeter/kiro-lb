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

PENDING = {
    "status": "authorization_pending",
    "accessToken": None,
    "refreshToken": None,
    "profileArn": None,
    "identityProvider": None,
}


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


# =============================================================================
# Builder ID (AWS SSO OIDC): pending is an HTTP 400 error code, not a 200 status
# =============================================================================

REGISTRATION = {"clientId": "client-id-value", "clientSecret": "client-secret-value"}
OIDC_AUTHORIZATION = {
    "deviceCode": "oidc-device-code",
    "userCode": "ABCD-EFGH",
    "verificationUri": "https://device.sso.us-east-1.amazonaws.com/",
    "verificationUriComplete": "https://device.sso.us-east-1.amazonaws.com/?user_code=ABCD-EFGH",
    "expiresIn": 600,
    "interval": 5,
}
OIDC_TOKEN = {
    "accessToken": "oidc-access-token",
    "refreshToken": "oidc-refresh-token-long-enough-for-validation",
    "expiresIn": 3600,
}


def test_builder_id_provider_aliases_resolve():
    assert resolve_provider("builder-id") == "BuilderId"
    assert resolve_provider("builder_id") == "BuilderId"
    assert resolve_provider("BuilderID") == "BuilderId"


@pytest.mark.asyncio
async def test_builder_id_start_registers_a_client_then_authorizes():
    client = _client(_Response(REGISTRATION), _Response(OIDC_AUTHORIZATION))
    with patch("kiro.device_login.httpx.AsyncClient", return_value=client):
        flow = await start_device_login("BuilderId")

    assert flow.provider == "BuilderId"
    assert flow.user_code == "ABCD-EFGH"
    # Seconds here, unlike the social flow's milliseconds.
    assert flow.interval_seconds == 5
    assert 590 < flow.expires_at - time.time() <= 600
    assert flow.registration is not None
    assert flow.registration["clientId"] == "client-id-value"


@pytest.mark.asyncio
async def test_builder_id_view_hides_the_client_secret():
    client = _client(_Response(REGISTRATION), _Response(OIDC_AUTHORIZATION))
    with patch("kiro.device_login.httpx.AsyncClient", return_value=client):
        flow = await start_device_login("BuilderId")

    assert "client-secret-value" not in str(flow.view())


@pytest.mark.asyncio
async def test_builder_id_pending_is_an_http_400_error_code():
    """AWS signals pending by failing the call. Code that only inspects a status
    field on a 200 response would treat this as a hard failure."""
    client = _client(_Response(REGISTRATION), _Response(OIDC_AUTHORIZATION))
    with patch("kiro.device_login.httpx.AsyncClient", return_value=client):
        flow = await start_device_login("BuilderId")

    pending = _client(_Response({"error": "authorization_pending"}, status_code=400))
    with patch("kiro.device_login.httpx.AsyncClient", return_value=pending):
        polled = await poll_device_login(flow.id)

    assert polled.status == "pending"
    assert polled.token is None


@pytest.mark.asyncio
async def test_builder_id_slow_down_backs_off_without_failing():
    client = _client(_Response(REGISTRATION), _Response(OIDC_AUTHORIZATION))
    with patch("kiro.device_login.httpx.AsyncClient", return_value=client):
        flow = await start_device_login("BuilderId")

    slow = _client(_Response({"error": "slow_down"}, status_code=400))
    with patch("kiro.device_login.httpx.AsyncClient", return_value=slow):
        polled = await poll_device_login(flow.id)

    assert polled.status == "pending"
    assert polled.interval_seconds == 10


@pytest.mark.asyncio
async def test_builder_id_expired_code_ends_the_flow():
    client = _client(_Response(REGISTRATION), _Response(OIDC_AUTHORIZATION))
    with patch("kiro.device_login.httpx.AsyncClient", return_value=client):
        flow = await start_device_login("BuilderId")

    expired = _client(_Response({"error": "expired_token"}, status_code=400))
    with patch("kiro.device_login.httpx.AsyncClient", return_value=expired):
        polled = await poll_device_login(flow.id)

    assert polled.status == "expired"


@pytest.mark.asyncio
async def test_builder_id_approval_carries_no_profile_arn():
    """Builder ID cannot obtain a profile, and an empty one fails the request, so
    the account must carry none at all to reach q.amazonaws.com."""
    client = _client(_Response(REGISTRATION), _Response(OIDC_AUTHORIZATION))
    with patch("kiro.device_login.httpx.AsyncClient", return_value=client):
        flow = await start_device_login("BuilderId")

    with patch("kiro.device_login.httpx.AsyncClient", return_value=_client(_Response(OIDC_TOKEN))):
        polled = await poll_device_login(flow.id)

    assert polled.status == "approved"
    assert polled.token is not None
    assert polled.token["refreshToken"] == OIDC_TOKEN["refreshToken"]
    assert polled.token["profileArn"] is None


@pytest.mark.asyncio
async def test_builder_id_credentials_file_carries_the_client_registration(tmp_path):
    client = _client(_Response(REGISTRATION), _Response(OIDC_AUTHORIZATION))
    with patch("kiro.device_login.httpx.AsyncClient", return_value=client):
        flow = await start_device_login("BuilderId")
    with patch("kiro.device_login.httpx.AsyncClient", return_value=_client(_Response(OIDC_TOKEN))):
        await poll_device_login(flow.id)

    path = device_login.write_builder_id_credentials(flow, tmp_path / "logins")
    document = __import__("json").loads(path.read_text())

    # clientId/clientSecret are what make auth.py pick the OIDC refresh endpoint.
    assert document["clientId"] == "client-id-value"
    assert document["clientSecret"] == "client-secret-value"
    assert document["refreshToken"] == OIDC_TOKEN["refreshToken"]
    assert document["region"] == "us-east-1"
    assert "profileArn" not in document
    assert oct(path.stat().st_mode)[-3:] == "600"


@pytest.mark.asyncio
async def test_registering_builder_id_adds_a_json_account(dashboard, tmp_path):
    client = _client(_Response(REGISTRATION), _Response(OIDC_AUTHORIZATION))
    with patch("kiro.device_login.httpx.AsyncClient", return_value=client):
        flow = await start_device_login("BuilderId")
    with patch("kiro.device_login.httpx.AsyncClient", return_value=_client(_Response(OIDC_TOKEN))):
        await poll_device_login(flow.id)

    request, manager = _request_with_manager(tmp_path)
    with patch.object(manager, "_initialize_account", AsyncMock(return_value=True)):
        result = await dashboard.dashboard_register_device_login(flow.id, request)

    assert result["provider"] == "BuilderId"
    entries = __import__("json").loads((tmp_path / "credentials.json").read_text())
    assert entries[0]["type"] == "json"
    assert "profile_arn" not in entries[0]


@pytest.mark.asyncio
async def test_writing_credentials_for_an_unapproved_flow_is_refused(tmp_path):
    client = _client(_Response(REGISTRATION), _Response(OIDC_AUTHORIZATION))
    with patch("kiro.device_login.httpx.AsyncClient", return_value=client):
        flow = await start_device_login("BuilderId")

    with pytest.raises(ValueError):
        device_login.write_builder_id_credentials(flow, tmp_path / "logins")


# =============================================================================
# Host routing: an account with no profile must not use the runtime host
# =============================================================================


def test_builder_id_host_differs_from_the_profile_host():
    from kiro.config import get_kiro_api_host, get_kiro_q_host

    assert get_kiro_api_host("us-east-1", is_builder_id=False) == "https://runtime.us-east-1.kiro.dev"
    assert get_kiro_api_host("us-east-1", is_builder_id=True) == "https://q.us-east-1.amazonaws.com"
    assert get_kiro_q_host("us-east-1", is_builder_id=True) == "https://q.us-east-1.amazonaws.com"


def test_host_selection_defaults_to_the_profile_host():
    """Callers that predate Builder ID must keep their existing behaviour."""
    from kiro.config import get_kiro_api_host

    assert get_kiro_api_host("eu-central-1") == "https://runtime.eu-central-1.kiro.dev"


def test_builder_id_credentials_route_to_the_q_host(tmp_path):
    """A Builder ID account sent to runtime.kiro.dev fails every request with
    400 profileArn is required, which is what this routing prevents."""
    import json as _json

    from kiro.auth import AuthType, KiroAuthManager

    path = tmp_path / "builder.json"
    path.write_text(
        _json.dumps(
            {
                "refreshToken": "refresh-token-value-long-enough",
                "accessToken": "access-token",
                "region": "us-east-1",
                "clientId": "client-id",
                "clientSecret": "client-secret",
                "startUrl": "https://view.awsapps.com/start",
            }
        )
    )

    manager = KiroAuthManager(creds_file=str(path))

    assert manager.auth_type == AuthType.AWS_SSO_OIDC
    assert manager.profile_arn is None
    assert manager.api_host == "https://q.us-east-1.amazonaws.com"


def test_a_social_account_without_a_profile_keeps_the_runtime_host(tmp_path):
    """Absence of a profile alone is not Builder ID: a Kiro Desktop account
    configured without one still belongs on the runtime host."""
    from kiro.auth import AuthType, KiroAuthManager

    manager = KiroAuthManager(refresh_token="social-refresh-token", region="us-east-1")

    assert manager.auth_type == AuthType.KIRO_DESKTOP
    assert manager.profile_arn is None
    assert manager.api_host == "https://runtime.us-east-1.kiro.dev"


def test_an_account_with_a_profile_keeps_the_runtime_host(tmp_path):
    import json as _json

    from kiro.auth import KiroAuthManager

    path = tmp_path / "social.json"
    path.write_text(
        _json.dumps(
            {
                "refreshToken": "refresh-token-value-long-enough",
                "accessToken": "access-token",
                "region": "us-east-1",
            }
        )
    )

    manager = KiroAuthManager(
        creds_file=str(path),
        profile_arn="arn:aws:codewhisperer:us-east-1:699475941385:profile/EHGA3GRVQMUK",
    )

    assert manager.api_host == "https://runtime.us-east-1.kiro.dev"
