# -*- coding: utf-8 -*-
"""/v1 5xx and 503 bodies must not leak pool dumps or exception text."""

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from loguru import logger

from kiro.account_manager import Account, AccountManager
from kiro.exceptions import (
    CLIENT_INTERNAL_ERROR_MESSAGE,
    CLIENT_UNAVAILABLE_MESSAGE,
    http_exception_handler,
)
from kiro.models_anthropic import AnthropicMessagesRequest
from kiro.models_openai import ChatCompletionRequest
from kiro.models_responses import ResponsesRequest
from kiro.routes_anthropic import messages
from kiro.routes_openai import chat_completions, responses
from kiro.streaming_responses import translate_chat_stream_to_responses

_LEAK_NEEDLES = ("Pool state", "RuntimeError", "SecretBoom", "cooling down for")


def _exhausted_manager(account_count: int = 3) -> AccountManager:
    manager = AccountManager()
    now = time.time()
    for index in range(account_count):
        account_id = f"/creds/account{index}.json"
        account = Account(id=account_id)
        account.auth_manager = MagicMock()
        account.failures = 2
        account.last_failure_time = now
        manager._accounts[account_id] = account
    return manager


def _request(manager: AccountManager) -> MagicMock:
    request = MagicMock()
    request.app.state.account_manager = manager
    request.app.state.http_client = MagicMock()
    return request


def _no_probabilistic_retry():
    return patch("kiro.account_manager.random.random", return_value=1.0)


def _chat_request() -> ChatCompletionRequest:
    return ChatCompletionRequest(model="claude-sonnet-4-5", messages=[{"role": "user", "content": "hi"}], stream=False)


def _anthropic_request() -> AnthropicMessagesRequest:
    return AnthropicMessagesRequest(
        model="claude-sonnet-4-5", max_tokens=64, messages=[{"role": "user", "content": "hi"}], stream=False
    )


def _capture_logs() -> tuple[list[str], int]:
    lines: list[str] = []
    sink_id = logger.add(lambda message: lines.append(message.record["message"]), format="{message}")
    return lines, sink_id


def _assert_client_safe(text: str) -> None:
    for needle in _LEAK_NEEDLES:
        assert needle not in text, f"client body leaked {needle!r}: {text}"
    assert "/creds/" not in text


class TestPoolExhaustedClientBodies:
    """Forced empty-pool 503s stay short on every /v1 surface."""

    @pytest.mark.asyncio
    async def test_openai_503_omits_pool_state(self):
        """
        What it does: Drives /v1/chat/completions against a fully cooling pool.
        Purpose: The 503 detail must not name accounts or dump pool state.
        """
        manager = _exhausted_manager()
        lines, sink_id = _capture_logs()
        try:
            with _no_probabilistic_retry():
                with pytest.raises(HTTPException) as exc_info:
                    await chat_completions(_request(manager), _chat_request())
        finally:
            logger.remove(sink_id)

        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == CLIENT_UNAVAILABLE_MESSAGE
        _assert_client_safe(str(exc_info.value.detail))
        assert any("Pool state" in line for line in lines)

    @pytest.mark.asyncio
    async def test_anthropic_503_omits_pool_state(self):
        """
        What it does: Drives /v1/messages against a fully cooling pool.
        Purpose: Anthropic JSON must keep api_error and drop the dump.
        """
        manager = _exhausted_manager()
        lines, sink_id = _capture_logs()
        try:
            with _no_probabilistic_retry():
                response = await messages(_request(manager), _anthropic_request())
        finally:
            logger.remove(sink_id)

        body = json.loads(bytes(response.body))
        assert response.status_code == 503
        assert body["type"] == "error"
        assert body["error"]["type"] == "api_error"
        assert body["error"]["message"] == CLIENT_UNAVAILABLE_MESSAGE
        _assert_client_safe(body["error"]["message"])
        assert any("Pool state" in line for line in lines)

    @pytest.mark.asyncio
    async def test_responses_reuses_the_sanitized_chat_503(self):
        """
        What it does: Drives /v1/responses through chat_completions on an empty pool.
        Purpose: The facade must not reintroduce the dump on the shared path.
        """
        manager = _exhausted_manager()
        request_data = ResponsesRequest(model="gpt-5.6-luna", input="hi", stream=False)
        with _no_probabilistic_retry():
            with pytest.raises(HTTPException) as exc_info:
                await responses(_request(manager), request_data)

        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == CLIENT_UNAVAILABLE_MESSAGE
        _assert_client_safe(str(exc_info.value.detail))


class TestInternalErrorClientBodies:
    """A forced generic exception must not echo class or message text."""

    def _serving_manager(self) -> tuple[AccountManager, MagicMock]:
        account = MagicMock()
        account.id = "/creds/boom.json"
        account.auth_manager = MagicMock()
        account.auth_manager.request_profile_arn = None
        account.auth_manager.generation_url = "https://example.test/generate"
        account.model_cache = MagicMock()
        manager = MagicMock()
        manager._accounts = {account.id: account, "/creds/other.json": account}
        manager.get_next_account = AsyncMock(return_value=account)
        return manager, account

    @pytest.mark.asyncio
    async def test_openai_500_omits_exception_text(self):
        """
        What it does: Forces request_with_retry to raise RuntimeError on chat.
        Purpose: The 500 detail is a short string, not Internal Server Error: {exc}.
        """
        manager, _account = self._serving_manager()
        leak = "SecretBoom RuntimeError Pool state: leaked"
        http_client = AsyncMock()
        http_client.request_with_retry = AsyncMock(side_effect=RuntimeError(leak))
        http_client.close = AsyncMock()

        with (
            patch("kiro.routes_openai.run_in_worker", AsyncMock(return_value={"conversationState": {}})),
            patch("kiro.routes_openai.KiroHttpClient", return_value=http_client),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await chat_completions(_request(manager), _chat_request())

        assert exc_info.value.status_code == 500
        assert exc_info.value.detail == CLIENT_INTERNAL_ERROR_MESSAGE
        _assert_client_safe(str(exc_info.value.detail))

    @pytest.mark.asyncio
    async def test_anthropic_500_omits_exception_text(self):
        """
        What it does: Forces request_with_retry to raise RuntimeError on messages.
        Purpose: Anthropic 500 JSON must keep api_error without the exception.
        """
        manager, _account = self._serving_manager()
        leak = "SecretBoom RuntimeError Pool state: leaked"
        http_client = AsyncMock()
        http_client.request_with_retry = AsyncMock(side_effect=RuntimeError(leak))
        http_client.close = AsyncMock()

        with (
            patch("kiro.routes_anthropic.run_in_worker", AsyncMock(return_value=MagicMock(payload={}, input_tokens=1))),
            patch("kiro.routes_anthropic.KiroHttpClient", return_value=http_client),
        ):
            response = await messages(_request(manager), _anthropic_request())

        body = json.loads(bytes(response.body))
        assert response.status_code == 500
        assert body["type"] == "error"
        assert body["error"]["type"] == "api_error"
        assert body["error"]["message"] == CLIENT_INTERNAL_ERROR_MESSAGE
        _assert_client_safe(body["error"]["message"])


class TestResponsesStreamErrorMessage:
    """response.failed must keep its code mapping and drop str(exc)."""

    @pytest.mark.asyncio
    async def test_mid_stream_failure_uses_a_safe_message(self):
        """
        What it does: Raises RuntimeError after the first chat chunk.
        Purpose: Codex reads error.message; it must not see the exception text.
        """

        async def exploding():
            raise RuntimeError("SecretBoom Pool state: leaked")
            yield  # pragma: no cover - generator shape only

        events = []
        async for raw in translate_chat_stream_to_responses(exploding(), model="gpt-5.6-luna"):
            for line in raw.splitlines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:") :].strip()))

        failed = events[-1]
        assert failed["type"] == "response.failed"
        assert failed["response"]["error"]["code"] == "server_error"
        assert failed["response"]["error"]["message"] == CLIENT_INTERNAL_ERROR_MESSAGE
        _assert_client_safe(failed["response"]["error"]["message"])

    @pytest.mark.asyncio
    async def test_rate_limit_keeps_the_retryable_code(self):
        """
        What it does: Raises HTTP 429 mid-stream.
        Purpose: The code mapping stays; only the message is sanitized.
        """

        async def rate_limited():
            raise HTTPException(status_code=429, detail="SecretBoom Pool state: leaked")
            yield  # pragma: no cover - generator shape only

        events = []
        async for raw in translate_chat_stream_to_responses(rate_limited(), model="gpt-5.6-luna"):
            for line in raw.splitlines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:") :].strip()))

        error = events[-1]["response"]["error"]
        assert error["code"] == "rate_limit_exceeded"
        _assert_client_safe(error["message"])


class TestHandlerStripsLegacyLeakyDetails:
    """Defense in depth if a route still raises a dump-shaped HTTPException."""

    @pytest.mark.asyncio
    async def test_openai_handler_strips_pool_state_from_503(self):
        """
        What it does: Sends a leaky 503 detail through the /v1 handler.
        Purpose: The shaped OpenAI envelope must not forward the dump.
        """
        request = MagicMock()
        request.url = MagicMock()
        request.url.path = "/v1/chat/completions"
        response = await http_exception_handler(
            request,
            HTTPException(
                status_code=503,
                detail="No available accounts for this model. Pool state: a1: cooling down for 30s.",
            ),
        )
        body = json.loads(response.body.decode())
        assert response.status_code == 503
        assert body["error"]["type"] == "api_error"
        assert body["error"]["message"] == CLIENT_UNAVAILABLE_MESSAGE
        _assert_client_safe(body["error"]["message"])

    @pytest.mark.asyncio
    async def test_control_plane_keeps_operator_detail(self):
        """
        What it does: Sends the same dump through a dashboard path.
        Purpose: Operators still see the full detail off the data plane.
        """
        request = MagicMock()
        request.url = MagicMock()
        request.url.path = "/api/dashboard/accounts"
        detail = "Pool state: a1: cooling down for 30s."
        response = await http_exception_handler(request, HTTPException(status_code=503, detail=detail))
        body = json.loads(response.body.decode())
        assert response.status_code == 503
        assert body["detail"] == detail
