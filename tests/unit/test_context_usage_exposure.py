# -*- coding: utf-8 -*-

"""Upstream context usage must reach the client.

Kiro reports no token counts at all. The only usage figure it sends is
contextUsagePercentage, and the gateway converted it to tokens and discarded the
percentage, so a client could never see the one upstream-sourced number available.
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


class TestOpenAIExposesContextUsage:
    @pytest.mark.asyncio
    async def test_streaming_final_chunk_carries_percentage(self, monkeypatch):
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
        assert usages[-1]["context_usage_percentage"] == 42.0


class TestAnthropicExposesContextUsage:
    @pytest.mark.asyncio
    async def test_streaming_message_delta_carries_percentage(self, monkeypatch):
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
        assert deltas[-1]["usage"]["context_usage_percentage"] == 37.5


class TestNonStreamingCarriesContextUsage:
    """Non-streaming must expose it too: the project rule is that any new
    client-visible behavior lands on both adapters, streaming and non-streaming."""

    @pytest.mark.asyncio
    async def test_openai_non_streaming_usage_carries_percentage(self, monkeypatch):
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

        assert result["usage"]["context_usage_percentage"] == 42.0

    @pytest.mark.asyncio
    async def test_anthropic_non_streaming_usage_carries_percentage(self, monkeypatch):
        import kiro.streaming_anthropic as sa
        from kiro.streaming_core import StreamResult

        # collect_anthropic_response goes through collect_stream_to_result, so the
        # percentage must survive that hop into the returned usage object.
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

        assert result["usage"]["context_usage_percentage"] == 37.5
