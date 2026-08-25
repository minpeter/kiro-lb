# -*- coding: utf-8 -*-

"""Generation-request contract shared by both protocol routers."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from kiro.account_manager import Account, AccountManager
from kiro.auth import AuthType
from kiro.cache import ModelInfoCache
from kiro.config import FALLBACK_MODELS, KIRO_BUILDER_ID_PROFILE_ARN
from kiro.model_resolver import ModelResolver

_MODEL = "claude-sonnet-4.5"
_RUNTIME_HOST = "https://runtime.us-east-1.kiro.dev"
_BODIES = {
    "openai": ("/v1/chat/completions", {"model": _MODEL, "messages": [{"role": "user", "content": "hi"}]}),
    "anthropic": (
        "/v1/messages",
        {"model": _MODEL, "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]},
    ),
}


class _CapturingHttpClient:
    """Record the final upstream call, then abort before stream handling."""

    calls: list = []

    def __init__(self, auth_manager, shared_client=None):
        self.auth_manager = auth_manager
        self.client = AsyncMock()

    async def request_with_retry(
        self,
        method,
        url,
        json_data=None,
        params=None,
        stream=False,
        retry_rate_limits=None,
    ):
        from kiro.utils import get_kiro_headers

        type(self).calls.append(
            {
                "method": method,
                "url": url,
                "payload": json_data,
                "headers": get_kiro_headers(self.auth_manager, "probe-token"),
            }
        )
        raise RuntimeError("contract probe: captured the upstream request")

    async def close(self):
        return None


def _account(account_id: str, *, profile_arn, auth_type) -> Account:
    account = Account(id=account_id)
    auth = AsyncMock()
    auth.get_access_token = AsyncMock(return_value="probe-token")
    auth.auth_type = auth_type
    auth.profile_arn = profile_arn
    if profile_arn:
        auth.request_profile_arn = profile_arn
    elif auth_type == AuthType.AWS_SSO_OIDC:
        auth.request_profile_arn = KIRO_BUILDER_ID_PROFILE_ARN
    else:
        auth.request_profile_arn = None
    auth.api_host = _RUNTIME_HOST
    auth.generation_url = f"{_RUNTIME_HOST}/"
    auth.q_host = _RUNTIME_HOST
    auth.region = "us-east-1"
    auth.fingerprint = "probe-fingerprint"

    account.auth_manager = auth
    account.model_cache = ModelInfoCache()
    asyncio.run(account.model_cache.update(FALLBACK_MODELS))
    account.model_resolver = ModelResolver(
        cache=account.model_cache,
        hidden_models={},
        aliases={},
        hidden_from_list=set(),
    )
    account.models_cached_at = float("inf")
    return account


@pytest.fixture
def capture_generation_request(clean_app, valid_proxy_api_key):
    from fastapi.testclient import TestClient

    def _capture(protocol: str, account: Account) -> dict:
        path, body = _BODIES[protocol]
        manager = AccountManager()
        manager._accounts = {account.id: account}

        _CapturingHttpClient.calls.clear()
        with (
            patch("kiro.routes_openai.KiroHttpClient", _CapturingHttpClient),
            patch("kiro.routes_anthropic.KiroHttpClient", _CapturingHttpClient),
            patch.object(AccountManager, "load_credentials", AsyncMock(return_value=None)),
            patch.object(AccountManager, "load_state", AsyncMock(return_value=None)),
            patch.object(AccountManager, "_initialize_account", AsyncMock(return_value=True)),
            patch.object(AccountManager, "save_state_periodically", AsyncMock(return_value=None)),
            patch.object(AccountManager, "_save_state", AsyncMock(return_value=None)),
        ):
            with TestClient(clean_app, raise_server_exceptions=False) as client:
                client.app.state.account_manager = manager
                headers = (
                    {"Authorization": f"Bearer {valid_proxy_api_key}"}
                    if protocol == "openai"
                    else {"x-api-key": valid_proxy_api_key}
                )
                client.post(path, headers=headers, json={**body, "stream": False})

        assert len(_CapturingHttpClient.calls) == 1
        return _CapturingHttpClient.calls[0]

    return _capture


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_generation_posts_to_the_runtime_root(capture_generation_request, protocol):
    call = capture_generation_request(
        protocol,
        _account("/creds/builder.json", profile_arn=None, auth_type=AuthType.AWS_SSO_OIDC),
    )

    assert call["method"] == "POST"
    assert call["url"] == f"{_RUNTIME_HOST}/"
    assert "generateAssistantResponse" not in call["url"]


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_builder_id_sends_the_cli_fallback_profile_without_persisting_it(capture_generation_request, protocol):
    account = _account("/creds/builder.json", profile_arn=None, auth_type=AuthType.AWS_SSO_OIDC)

    call = capture_generation_request(protocol, account)

    assert call["payload"]["profileArn"] == KIRO_BUILDER_ID_PROFILE_ARN
    assert account.auth_manager.profile_arn is None


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_an_account_with_a_profile_sends_its_own(capture_generation_request, protocol):
    own = "arn:aws:codewhisperer:us-east-1:123456789012:profile/own"

    call = capture_generation_request(
        protocol,
        _account("/creds/social.json", profile_arn=own, auth_type=AuthType.KIRO_DESKTOP),
    )

    assert call["payload"]["profileArn"] == own


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_a_profileless_account_never_receives_the_builder_id_fallback(capture_generation_request, protocol):
    call = capture_generation_request(
        protocol,
        _account("/creds/noprofile.json", profile_arn=None, auth_type=AuthType.KIRO_DESKTOP),
    )

    assert "profileArn" not in call["payload"]


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_generation_headers_carry_the_cli_retry_contract(capture_generation_request, protocol):
    call = capture_generation_request(
        protocol,
        _account("/creds/builder.json", profile_arn=None, auth_type=AuthType.AWS_SSO_OIDC),
    )
    headers = call["headers"]

    assert headers["x-amz-target"] == "AmazonCodeWhispererStreamingService.GenerateAssistantResponse"
    assert headers["x-kiro-attempt"] == "1;max=3"
    assert "x-amzn-kiro-agent-mode" not in headers
    assert "app/AmazonQ-For-CLI" in headers["User-Agent"]
    assert "KiroIDE" not in headers["User-Agent"]
