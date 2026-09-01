"""A bracket-style call must not leak as text nor be published twice, on any of
the three paths that convert a Kiro stream."""

import json

import pytest

import kiro.streaming_core as streaming_core
import kiro.streaming_openai as streaming_openai
from kiro.streaming_core import KiroEvent, collect_stream_to_result

CALL = '[Called Write with args: {"file_path": "/tmp/a.py", "content": "print(1)"}]'
NATIVE = {
    "id": "toolu_1",
    "type": "function",
    "function": {"name": "Write", "arguments": '{"file_path": "/tmp/a.py", "content": "print(1)"}'},
}


class _FakeResponse:
    status_code = 200
    headers: dict = {}

    async def aclose(self):
        pass


def _patch_stream(monkeypatch, module, events):
    async def fake_parse(*args, **kwargs):
        for event in events:
            yield event

    monkeypatch.setattr(module, "parse_kiro_stream", fake_parse)


class TestCollector:
    @pytest.mark.asyncio
    async def test_a_call_in_text_does_not_stay_in_the_content(self, monkeypatch):
        _patch_stream(monkeypatch, streaming_core, [KiroEvent(type="content", content="Vou criar.\n" + CALL)])

        result = await collect_stream_to_result(_FakeResponse())

        assert result.content == "Vou criar.\n"
        assert "[Called" not in result.content
        assert len(result.tool_calls) == 1
        assert [block["type"] for block in result.content_blocks] == ["text", "tool_use"]

    @pytest.mark.asyncio
    async def test_a_structured_call_echoed_in_text_is_collected_once(self, monkeypatch):
        _patch_stream(
            monkeypatch,
            streaming_core,
            [
                KiroEvent(type="content", content="Criando.\n"),
                KiroEvent(type="tool_use", tool_use=NATIVE),
                KiroEvent(type="content", content=CALL),
            ],
        )

        result = await collect_stream_to_result(_FakeResponse())

        assert len(result.tool_calls) == 1, "the echo must not become a second call"
        assert [block["type"] for block in result.content_blocks] == ["text", "tool_use"]
        assert "[Called" not in result.content


class TestOpenAiStream:
    async def _run(self, monkeypatch, events):
        from unittest.mock import MagicMock

        _patch_stream(monkeypatch, streaming_openai, events)
        cache = MagicMock()
        cache.get_token_limits.return_value = {"maxInputTokens": 200000, "maxOutputTokens": 8192}

        texts: list = []
        names: list = []
        async for raw in streaming_openai.stream_kiro_to_openai(
            MagicMock(), _FakeResponse(), "claude-opus-5", cache, MagicMock(), request_messages=[]
        ):
            for line in raw.splitlines():
                if not line.startswith("data: ") or line.strip() == "data: [DONE]":
                    continue
                delta = json.loads(line[6:]).get("choices", [{}])[0].get("delta", {})
                if delta.get("content"):
                    texts.append(delta["content"])
                for call in delta.get("tool_calls") or []:
                    names.append(call.get("function", {}).get("name"))
        return "".join(texts), names

    @pytest.mark.asyncio
    async def test_a_call_in_text_is_not_sent_as_content(self, monkeypatch):
        pieces = [CALL[i : i + 9] for i in range(0, len(CALL), 9)]
        events = [KiroEvent(type="content", content="Vou criar.\n")]
        events += [KiroEvent(type="content", content=piece) for piece in pieces]

        text, names = await self._run(monkeypatch, events)

        assert "[Called" not in text
        assert text == "Vou criar.\n"
        assert names == ["Write"]

    @pytest.mark.asyncio
    async def test_a_structured_call_echoed_in_text_is_emitted_once(self, monkeypatch):
        events = [
            KiroEvent(type="content", content="Criando.\n"),
            KiroEvent(type="tool_use", tool_use=NATIVE),
            KiroEvent(type="content", content=CALL),
        ]

        text, names = await self._run(monkeypatch, events)

        assert names == ["Write"], "a call already streamed must not be repeated"
        assert "[Called" not in text
