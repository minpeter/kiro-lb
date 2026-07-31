# -*- coding: utf-8 -*-

"""The back-calculated token counts must reach the client, in spec fields only.

Kiro reports no token counts at all. The only usage figure it sends is
contextUsagePercentage, which the gateway converts into a token count. That
converted number is what clients must see, carried in the fields their protocol
already defines - never as an extra `context_usage_percentage` key, which would
make the response a non-standard dialect of OpenAI and Anthropic usage objects.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro.streaming_core import KiroEvent


def _cache(max_input=1000000):
    cache = MagicMock()
    cache.get_max_input_tokens.return_value = max_input
    return cache


def _events(pct=42.0):
    return [
        KiroEvent(type="content", content="hi"),
        KiroEvent(type="context_usage", context_usage_percentage=pct),
    ]


async def _agen(items):
    for item in items:
        yield item


def _expected_prompt_tokens(pct, max_input, completion_tokens):
    return int((pct / 100) * max_input) - completion_tokens


class TestOpenAIUsageStaysStandard:
    @pytest.mark.asyncio
    async def test_streaming_final_chunk_carries_derived_tokens_only(self, monkeypatch):
        import kiro.streaming_openai as so

        monkeypatch.setattr(so, "parse_kiro_stream", lambda *a, **k: _agen(_events(42.0)))

        response = AsyncMock()
        response.status_code = 200
        chunks = []
        async for chunk in so.stream_kiro_to_openai_internal(
            MagicMock(),
            response,
            "claude-opus-4.7",
            _cache(),
            MagicMock(),
            request_messages=[{"role": "user", "content": "x"}],
        ):
            chunks.append(chunk)

        payloads = []
        for chunk in chunks:
            for line in chunk.splitlines():
                if line.startswith("data: ") and "[DONE]" not in line:
                    payloads.append(json.loads(line[6:]))

        usages = [p["usage"] for p in payloads if p.get("usage")]
        assert usages, "no usage object emitted"
        usage = usages[-1]
        assert "context_usage_percentage" not in usage
        assert usage["total_tokens"] == int((42.0 / 100) * 1000000)
        assert usage["prompt_tokens"] == usage["total_tokens"] - usage["completion_tokens"]

    @pytest.mark.asyncio
    async def test_non_streaming_usage_carries_derived_tokens_only(self, monkeypatch):
        import kiro.streaming_openai as so

        # collect_stream_response consumes the internal generator, so that is the
        # seam: patching parse_kiro_stream would not survive the retry wrapper.
        async def fake_stream(*args, **kwargs):
            async for chunk in so.stream_kiro_to_openai_internal(*args, **kwargs):
                yield chunk

        monkeypatch.setattr(so, "parse_kiro_stream", lambda *a, **k: _agen(_events(42.0)))
        monkeypatch.setattr(so, "stream_kiro_to_openai", fake_stream)

        response = AsyncMock()
        response.status_code = 200
        result = await so.collect_stream_response(
            MagicMock(),
            response,
            "claude-opus-4.7",
            _cache(),
            MagicMock(),
            request_messages=[{"role": "user", "content": "x"}],
        )

        usage = result["usage"]
        assert "context_usage_percentage" not in usage
        assert usage["total_tokens"] == int((42.0 / 100) * 1000000)


class TestAnthropicUsageStaysStandard:
    @pytest.mark.asyncio
    async def test_streaming_message_delta_carries_derived_input_tokens(self, monkeypatch):
        import kiro.streaming_anthropic as sa

        monkeypatch.setattr(sa, "parse_kiro_stream", lambda *a, **k: _agen(_events(37.5)))

        response = AsyncMock()
        response.status_code = 200
        events = []
        async for chunk in sa.stream_kiro_to_anthropic(
            response, "claude-opus-4.7", _cache(), MagicMock(), request_messages=[{"role": "user", "content": "x"}]
        ):
            events.append(chunk)

        deltas = []
        for chunk in events:
            for line in chunk.splitlines():
                if line.startswith("data: "):
                    parsed = json.loads(line[6:])
                    if parsed.get("type") == "message_delta":
                        deltas.append(parsed)

        assert deltas, "no message_delta emitted"
        usage = deltas[-1]["usage"]
        assert "context_usage_percentage" not in usage
        expected = _expected_prompt_tokens(37.5, 1000000, usage["output_tokens"])
        assert usage["input_tokens"] == expected

    @pytest.mark.asyncio
    async def test_streaming_message_delta_omits_input_tokens_without_upstream_usage(self, monkeypatch):
        import kiro.streaming_anthropic as sa

        monkeypatch.setattr(sa, "parse_kiro_stream", lambda *a, **k: _agen([KiroEvent(type="content", content="hi")]))

        response = AsyncMock()
        response.status_code = 200
        deltas = []
        async for chunk in sa.stream_kiro_to_anthropic(
            response, "claude-opus-4.7", _cache(), MagicMock(), request_messages=[{"role": "user", "content": "x"}]
        ):
            for line in chunk.splitlines():
                if line.startswith("data: "):
                    parsed = json.loads(line[6:])
                    if parsed.get("type") == "message_delta":
                        deltas.append(parsed)

        assert deltas, "no message_delta emitted"
        assert "input_tokens" not in deltas[-1]["usage"]

    @pytest.mark.asyncio
    async def test_non_streaming_usage_carries_derived_tokens_only(self, monkeypatch):
        import kiro.streaming_anthropic as sa
        from kiro.streaming_core import StreamResult

        async def fake_collect(_response):
            return StreamResult(content="hi", context_usage_percentage=37.5)

        monkeypatch.setattr(sa, "collect_stream_to_result", fake_collect)

        response = AsyncMock()
        response.status_code = 200
        result = await sa.collect_anthropic_response(
            response,
            "claude-opus-4.7",
            _cache(),
            MagicMock(),
            request_messages=[{"role": "user", "content": "x"}],
        )

        usage = result["usage"]
        assert "context_usage_percentage" not in usage
        expected = _expected_prompt_tokens(37.5, 1000000, usage["output_tokens"])
        assert usage["input_tokens"] == expected
