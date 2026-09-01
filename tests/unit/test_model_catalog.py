# -*- coding: utf-8 -*-
"""The model catalogue comes from the account, not from a constant.

The runtime host has no /ListAvailableModels, so the gateway used a hardcoded
list. The operation is served by the management host instead, which is what the
official CLI calls, so the list, the token limits and the presentable names can
all come from the account.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro import model_catalog


def _auth(arn: str | None = "arn:aws:codewhisperer:eu-central-1:1:profile/X"):
    async def token():
        return "tok"

    return SimpleNamespace(
        get_access_token=token,
        profile_arn=arn,
        request_profile_arn=arn,
        api_region="us-east-1",
    )


class TestRegion:
    def test_prefers_the_arn_region(self):
        assert model_catalog._region(_auth()) == "eu-central-1"

    def test_falls_back_to_the_api_region(self):
        assert model_catalog._region(_auth(None)) == "us-east-1"

    def test_ignores_an_unrelated_arn(self):
        assert model_catalog._region(_auth("arn:aws:iam::1:user/x")) == "us-east-1"


class _Response:
    def __init__(self, status: int, payload: dict | None = None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def _client_returning(response, captured: dict):
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, url, params=None, json=None, headers=None):
            captured["url"] = url
            captured["params"] = params
            captured["body"] = json
            captured["headers"] = headers
            if isinstance(response, Exception):
                raise response
            return response

    return lambda **_: FakeClient()


@pytest.mark.asyncio
class TestFetchAvailableModels:
    @pytest.fixture(autouse=True)
    def no_headers(self, monkeypatch):
        monkeypatch.setattr(model_catalog, "get_kiro_headers", lambda *_: {})

    async def test_returns_the_model_list(self, monkeypatch):
        payload = {"models": [{"modelId": "claude-opus-5", "modelName": "Claude Opus 5"}]}
        captured: dict = {}
        monkeypatch.setattr(model_catalog.httpx, "AsyncClient", _client_returning(_Response(200, payload), captured))

        models = await model_catalog.fetch_available_models(_auth())
        assert models == payload["models"]

    async def test_sends_the_arn_in_query_and_body(self, monkeypatch):
        """The upstream rejects the call with "Invalid profileArn" without both."""
        captured: dict = {}
        monkeypatch.setattr(
            model_catalog.httpx,
            "AsyncClient",
            _client_returning(_Response(200, {"models": [{"modelId": "a"}]}), captured),
        )

        await model_catalog.fetch_available_models(_auth())
        assert captured["params"]["profileArn"].startswith("arn:")
        assert captured["body"]["profileArn"].startswith("arn:")
        assert captured["headers"]["x-amz-target"] == model_catalog.TARGET
        assert captured["url"].startswith("https://management.eu-central-1.kiro.dev")

    @pytest.mark.parametrize("status", [400, 403, 404, 500])
    async def test_non_200_keeps_the_previous_catalogue(self, monkeypatch, status):
        """None means "keep what you had"; emptying a working list is worse."""
        captured: dict = {}
        monkeypatch.setattr(model_catalog.httpx, "AsyncClient", _client_returning(_Response(status), captured))
        assert await model_catalog.fetch_available_models(_auth()) is None

    async def test_transport_failure_is_swallowed(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(model_catalog.httpx, "AsyncClient", _client_returning(RuntimeError("down"), captured))
        assert await model_catalog.fetch_available_models(_auth()) is None

    async def test_empty_list_is_not_adopted(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            model_catalog.httpx, "AsyncClient", _client_returning(_Response(200, {"models": []}), captured)
        )
        assert await model_catalog.fetch_available_models(_auth()) is None

    async def test_entries_without_an_id_are_dropped(self, monkeypatch):
        payload = {"models": [{"modelName": "no id"}, {"modelId": "ok"}, "junk"]}
        captured: dict = {}
        monkeypatch.setattr(model_catalog.httpx, "AsyncClient", _client_returning(_Response(200, payload), captured))
        assert await model_catalog.fetch_available_models(_auth()) == [{"modelId": "ok"}]

    async def test_a_missing_token_is_not_fatal(self, monkeypatch):
        async def boom():
            raise RuntimeError("no token")

        auth = _auth()
        auth.get_access_token = boom
        assert await model_catalog.fetch_available_models(auth) is None


class TestDisplayName:
    def test_uses_the_upstream_name(self):
        from kiro.routes_openai import _display_name

        assert _display_name("claude-opus-5", "Claude Opus 5") == "Claude Opus 5"

    def test_falls_back_for_a_local_alias(self):
        """auto-kiro is the gateway's own id, so the upstream has no name for it."""
        from kiro.routes_openai import _display_name

        assert _display_name("auto-kiro", None) == "auto-kiro (Kiro)"
