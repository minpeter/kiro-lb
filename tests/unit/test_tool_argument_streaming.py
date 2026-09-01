"""A client renders a pending tool call from the argument fragments, so both
adapters must forward one fragment per fragment upstream sent instead of the
assembled call in a single event."""

import json
from unittest.mock import MagicMock

import pytest

import kiro.streaming_anthropic as streaming_anthropic
import kiro.streaming_openai as streaming_openai
from kiro.parsers import AwsEventStreamParser
from kiro.streaming_core import KiroEvent, _process_chunk

ARGS = '{"file_path": "/tmp/app.py", "content": "def main():\\n    return 42\\n"}'
PIECES = [ARGS[i : i + 11] for i in range(0, len(ARGS), 11)]

FRAMES = [
    {"content": "Vou criar o arquivo.\n"},
    {"name": "Write", "toolUseId": "tool-1", "input": ""},
    *[{"input": piece, "toolUseId": "tool-1"} for piece in PIECES],
    {"stop": True, "toolUseId": "tool-1"},
    {"contextUsagePercentage": 12.5},
    {"stopReason": "TOOL_USE"},
]


class _FakeResponse:
    status_code = 200
    headers: dict = {}

    async def aclose(self):
        pass


async def _upstream():
    """Drive the real parser with the frame shapes Kiro sends."""
    parser = AwsEventStreamParser()
    for payload in FRAMES:
        async for event in _process_chunk(parser, json.dumps(payload).encode()):
            yield event
    for tool_call in parser.get_unemitted_tool_calls():
        yield KiroEvent(type="tool_use", tool_use=tool_call)


def _patch(monkeypatch, module):
    async def fake_parse(*args, **kwargs):
        async for event in _upstream():
            yield event

    monkeypatch.setattr(module, "parse_kiro_stream", fake_parse)
    cache = MagicMock()
    cache.get_token_limits.return_value = {"maxInputTokens": 200000, "maxOutputTokens": 8192}
    return cache


@pytest.mark.asyncio
async def test_anthropic_forwards_one_delta_per_upstream_fragment(monkeypatch):
    cache = _patch(monkeypatch, streaming_anthropic)

    fragments = []
    opened = 0
    async for raw in streaming_anthropic.stream_kiro_to_anthropic(
        _FakeResponse(), "claude-opus-5", cache, MagicMock(), request_messages=[], known_input_tokens=10
    ):
        for line in raw.splitlines():
            if not line.startswith("data: "):
                continue
            payload = json.loads(line[6:])
            if payload.get("delta", {}).get("type") == "input_json_delta":
                fragments.append(payload["delta"]["partial_json"])
            if payload.get("content_block", {}).get("type") == "tool_use":
                opened += 1

    assert len(fragments) == len(PIECES)
    assert opened == 1, "the block opens once, not once per fragment"
    assert "".join(fragments) == ARGS
    assert json.loads("".join(fragments))["file_path"] == "/tmp/app.py"


@pytest.mark.asyncio
async def test_openai_forwards_one_arguments_chunk_per_upstream_fragment(monkeypatch):
    cache = _patch(monkeypatch, streaming_openai)

    fragments = []
    names = []
    indices = set()
    async for raw in streaming_openai.stream_kiro_to_openai(
        MagicMock(), _FakeResponse(), "claude-opus-5", cache, MagicMock(), request_messages=[]
    ):
        for line in raw.splitlines():
            if not line.startswith("data: ") or line.strip() == "data: [DONE]":
                continue
            delta = json.loads(line[6:]).get("choices", [{}])[0].get("delta", {})
            for call in delta.get("tool_calls") or []:
                indices.add(call["index"])
                function = call.get("function", {})
                if function.get("name"):
                    names.append(function["name"])
                if function.get("arguments"):
                    fragments.append(function["arguments"])

    assert len(fragments) == len(PIECES)
    assert names == ["Write"], "the name is announced once, with the opening chunk"
    assert indices == {0}, "every chunk must carry the same index so the client appends"
    assert "".join(fragments) == ARGS
    assert json.loads("".join(fragments))["file_path"] == "/tmp/app.py"


@pytest.mark.asyncio
async def test_openai_buffers_when_only_one_call_may_be_returned(monkeypatch):
    """parallel_tool_calls=false drops the extra calls at the end, which is
    impossible once they are on the wire, so that case keeps buffering."""
    cache = _patch(monkeypatch, streaming_openai)

    fragments = []
    async for raw in streaming_openai.stream_kiro_to_openai(
        MagicMock(),
        _FakeResponse(),
        "claude-opus-5",
        cache,
        MagicMock(),
        request_messages=[],
        parallel_tool_calls=False,
    ):
        for line in raw.splitlines():
            if not line.startswith("data: ") or line.strip() == "data: [DONE]":
                continue
            delta = json.loads(line[6:]).get("choices", [{}])[0].get("delta", {})
            for call in delta.get("tool_calls") or []:
                if call.get("function", {}).get("arguments"):
                    fragments.append(call["function"]["arguments"])

    assert len(fragments) == 1, "buffered into a single chunk"
    assert json.loads(fragments[0])["file_path"] == "/tmp/app.py"
