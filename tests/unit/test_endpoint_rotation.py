# -*- coding: utf-8 -*-
"""Generation endpoint rotation tests."""

import httpx
import pytest

from kiro import endpoints as ep
from kiro.endpoint_settings import EndpointSettings
from kiro.http_client import KiroHttpClient


@pytest.fixture(autouse=True)
def clean_state():
    ep.reset_state()
    yield
    ep.reset_state()


class TestEndpointDefinitions:
    def test_declared_order_matches_the_configured_default(self):
        """A mismatch would try a different host depending on how it was reached."""
        from kiro.config import KIRO_ENDPOINT_ORDER

        assert [item.key for item in ep.selected_endpoints()] == KIRO_ENDPOINT_ORDER

    def test_runtime_keeps_the_generate_target(self):
        runtime = ep.ENDPOINTS_BY_KEY["runtime"]
        assert runtime.url("us-east-1") == "https://runtime.us-east-1.kiro.dev/"
        assert runtime.header_overrides()["x-amz-target"] == ep.GENERATE_TARGET

    def test_amazonq_uses_its_own_target(self):
        amazonq = ep.ENDPOINTS_BY_KEY["amazonq"]
        assert amazonq.header_overrides()["x-amz-target"] == "AmazonQDeveloperStreamingService.SendMessage"
        assert amazonq.url("eu-central-1") == "https://q.eu-central-1.amazonaws.com/generateAssistantResponse"

    def test_unknown_keys_are_ignored_and_never_empty(self):
        assert [e.key for e in ep.selected_endpoints(["amazonq", "nope", "runtime"])] == ["amazonq", "runtime"]
        # An all-unknown list falls back to the declared order rather than emptying.
        assert ep.selected_endpoints(["nope"])[0].key == ep.KIRO_ENDPOINTS[0].key


class TestAttemptOrder:
    def test_affinity_puts_the_working_endpoint_first(self):
        ep.record_success("acc", "claude-opus-5", "amazonq")
        assert ep.attempt_order("acc", "claude-opus-5")[0].key == "amazonq"

    def test_affinity_is_scoped_per_account_and_model(self):
        ep.record_success("acc", "claude-opus-5", "amazonq")
        assert ep.attempt_order("acc", "claude-sonnet-5")[0].key == ep.KIRO_ENDPOINTS[0].key
        assert ep.attempt_order("other", "claude-opus-5")[0].key == ep.KIRO_ENDPOINTS[0].key

    def test_cooling_endpoint_goes_last_but_is_never_dropped(self):
        ep.record_failure("runtime", cooldown_seconds=60)
        order = [e.key for e in ep.attempt_order("acc", "m")]
        assert order[-1] == "runtime", "a cooling endpoint must move to the back"
        assert len(order) == len(ep.KIRO_ENDPOINTS), "no endpoint may be dropped"

    def test_success_clears_the_cooldown(self):
        ep.record_failure("runtime", cooldown_seconds=60)
        assert ep.is_cooling("runtime")
        ep.record_success("acc", "m", "runtime")
        assert not ep.is_cooling("runtime")

    def test_zero_cooldown_is_a_noop(self):
        ep.record_failure("runtime", cooldown_seconds=0)
        assert not ep.is_cooling("runtime")


class _FakeAuth:
    api_region = "us-east-1"
    profile_arn = "arn:aws:codewhisperer:us-east-1:1:profile/TEST"
    generation_url = "https://runtime.us-east-1.kiro.dev/"


class _LiveFakeAuth(_FakeAuth):
    async def get_access_token(self):
        return "token"


def _wire_transport(monkeypatch, client, failing_markers):
    """Drive the real _attempt_endpoint loop against a transport that refuses
    connections for URLs containing any of ``failing_markers``."""
    calls: list[str] = []

    class _Transport:
        def build_request(self, method, url, **kwargs):
            return httpx.Request(method, url)

        async def send(self, req, stream=False):
            return await self.request(req.method, str(req.url))

        async def request(self, method, url, **kwargs):
            calls.append(url)
            if any(marker in url for marker in failing_markers):
                raise httpx.ConnectError("connection refused")
            return _response(200)

    async def fake_get_client(stream=False):
        return _Transport()

    monkeypatch.setattr(client, "_get_client", fake_get_client)
    monkeypatch.setattr("kiro.http_client.BASE_RETRY_DELAY", 0)
    monkeypatch.setattr("kiro.http_client.get_kiro_headers", lambda auth, token: {})
    return calls


_GENERATION_URL = _FakeAuth.generation_url
_PAYLOAD = {"conversationState": {"currentMessage": {"userInputMessage": {"modelId": "claude-opus-5"}}}}


def _client(monkeypatch, rotation=True):
    settings = EndpointSettings(
        rotation=rotation,
        order=("runtime", "codewhisperer", "amazonq"),
        cooldown_seconds=30.0,
    )
    monkeypatch.setattr("kiro.http_client.current_endpoint_settings", lambda: settings)
    client = KiroHttpClient.__new__(KiroHttpClient)
    client.auth_manager = _FakeAuth()
    return client


def _response(status):
    return httpx.Response(status_code=status, request=httpx.Request("POST", "https://example.invalid/"))


@pytest.mark.asyncio
class TestRotationBehavior:
    async def test_account_errors_do_not_rotate(self, monkeypatch):
        """A dead credential must not be retried on the other hosts.

        Rotating on it re-enters the 403 refresh retry once per remaining
        endpoint, which multiplies calls to the auth host and gets the gateway
        rate limited there.
        """
        from kiro.account_errors import CredentialDeadError

        client = _client(monkeypatch)
        attempts = []

        async def fake(method, url, **kwargs):
            attempts.append(url)
            raise CredentialDeadError("refresh rejected", 401)

        monkeypatch.setattr(client, "_attempt_endpoint", fake)

        with pytest.raises(CredentialDeadError):
            await client.request_with_retry("POST", _FakeAuth().generation_url, json_data={})

        assert len(attempts) == 1, "an account error must not be retried on another endpoint"

    async def test_transport_errors_still_rotate(self, monkeypatch):
        client = _client(monkeypatch)
        attempts = []

        async def fake(method, url, **kwargs):
            attempts.append(url)
            if len(attempts) < 3:
                raise httpx.ConnectError("boom")
            return _response(200)

        monkeypatch.setattr(client, "_attempt_endpoint", fake)

        response = await client.request_with_retry("POST", _FakeAuth().generation_url, json_data={})
        assert response.status_code == 200
        assert len(attempts) == 3, "a transport failure should still move to the next endpoint"

    async def test_rotates_past_a_5xx_and_records_the_winner(self, monkeypatch):
        client = _client(monkeypatch)
        seen = []

        async def fake_attempt(method, url, **kwargs):
            seen.append(url)
            return _response(500) if "runtime" in url else _response(200)

        client._attempt_endpoint = fake_attempt
        response = await client.request_with_retry("POST", _GENERATION_URL, _PAYLOAD)

        assert response.status_code == 200
        assert len(seen) == 2 and "runtime" in seen[0] and "codewhisperer" in seen[1]
        assert ep.attempt_order(_FakeAuth.profile_arn, "claude-opus-5")[0].key == "codewhisperer"
        assert ep.is_cooling("runtime")

    async def test_rotates_past_a_transport_error(self, monkeypatch):
        client = _client(monkeypatch)
        calls = []

        async def fake_attempt(method, url, **kwargs):
            calls.append(url)
            if "runtime" in url:
                raise httpx.ConnectError("boom")
            return _response(200)

        client._attempt_endpoint = fake_attempt
        response = await client.request_with_retry("POST", _GENERATION_URL, _PAYLOAD)
        assert response.status_code == 200
        assert len(calls) == 2

    async def test_429_returns_immediately_for_account_failover(self, monkeypatch):
        client = _client(monkeypatch)
        calls = []

        async def fake_attempt(method, url, **kwargs):
            calls.append(url)
            return _response(429)

        client._attempt_endpoint = fake_attempt
        response = await client.request_with_retry("POST", _GENERATION_URL, _PAYLOAD)
        assert response.status_code == 429
        assert len(calls) == 1, "a 429 belongs to the account, not the endpoint"
        assert not ep.is_cooling("runtime")

    async def test_400_does_not_rotate(self, monkeypatch):
        client = _client(monkeypatch)
        calls = []

        async def fake_attempt(method, url, **kwargs):
            calls.append(url)
            return _response(400)

        client._attempt_endpoint = fake_attempt
        response = await client.request_with_retry("POST", _GENERATION_URL, _PAYLOAD)
        assert response.status_code == 400
        assert len(calls) == 1, "an oversized payload would fail on every endpoint"

    async def test_disabled_rotation_uses_only_the_given_url(self, monkeypatch):
        client = _client(monkeypatch, rotation=False)
        calls = []

        async def fake_attempt(method, url, **kwargs):
            calls.append(url)
            return _response(500)

        client._attempt_endpoint = fake_attempt
        response = await client.request_with_retry("POST", _GENERATION_URL, _PAYLOAD)
        assert response.status_code == 500
        assert calls == [_GENERATION_URL]

    async def test_management_urls_are_never_rotated(self, monkeypatch):
        """ListAvailableModels is not generation: it must reach its own host."""
        client = _client(monkeypatch, rotation=True)
        calls = []

        async def fake_attempt(method, url, **kwargs):
            calls.append(url)
            return _response(500)

        client._attempt_endpoint = fake_attempt
        management = "https://runtime.us-east-1.kiro.dev/ListAvailableModels"
        response = await client.request_with_retry("GET", management)
        assert response.status_code == 500
        assert calls == [management]

    async def test_last_5xx_is_returned_when_every_endpoint_fails(self, monkeypatch):
        client = _client(monkeypatch)

        async def fake_attempt(method, url, **kwargs):
            return _response(503)

        client._attempt_endpoint = fake_attempt
        response = await client.request_with_retry("POST", _GENERATION_URL, _PAYLOAD)
        assert response.status_code == 503

    async def test_exhausted_transport_error_reaches_the_rotation_layer(self, monkeypatch):
        """Regression: _attempt_endpoint converted an exhausted transport error
        into HTTPException, which neither the proxy nor the endpoint fallback
        layer catches, so the request died on the first route with the others
        never tried. The real retry loop must let the raw error propagate."""
        client = _client(monkeypatch)
        client.auth_manager = _LiveFakeAuth()
        client.retry_rate_limits = False
        calls = _wire_transport(monkeypatch, client, failing_markers=("runtime",))

        response = await client.request_with_retry("POST", _GENERATION_URL, _PAYLOAD)

        assert response.status_code == 200
        assert any("runtime" in url for url in calls), "the first endpoint must actually be attempted"
        assert any("runtime" not in url for url in calls), "a second endpoint must be attempted after exhaustion"

    async def test_every_route_down_still_returns_an_http_exception(self, monkeypatch):
        from fastapi import HTTPException

        client = _client(monkeypatch)
        client.auth_manager = _LiveFakeAuth()
        client.retry_rate_limits = False
        _wire_transport(monkeypatch, client, failing_markers=("https",))

        with pytest.raises(HTTPException) as excinfo:
            await client.request_with_retry("POST", _GENERATION_URL, _PAYLOAD)
        assert excinfo.value.status_code in (502, 503, 504)

    async def test_per_endpoint_headers_are_passed_through(self, monkeypatch):
        client = _client(monkeypatch)
        captured = {}

        async def fake_attempt(method, url, **kwargs):
            captured[url] = kwargs.get("header_overrides")
            return _response(500) if "runtime" in url or "codewhisperer" in url else _response(200)

        client._attempt_endpoint = fake_attempt
        await client.request_with_retry("POST", _GENERATION_URL, _PAYLOAD)

        amazonq_url = ep.ENDPOINTS_BY_KEY["amazonq"].url("us-east-1")
        assert captured[amazonq_url]["x-amz-target"] == "AmazonQDeveloperStreamingService.SendMessage"
