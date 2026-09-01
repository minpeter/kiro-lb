"""Regressions found by review on the tool-argument streaming work."""

import json
from unittest.mock import MagicMock

import pytest

import kiro.streaming_anthropic as streaming_anthropic
import kiro.streaming_openai as streaming_openai
from kiro.parsers import AwsEventStreamParser
from kiro.streaming_core import KiroEvent, _process_chunk

CALL = '[Called Write with args: {"file_path": "/tmp/a.py", "content": "print(1)"}]'
ARGS = '{"file_path": "/tmp/app.py", "content": "def main():\\n    return 42\\n"}'
PIECES = [ARGS[i : i + 10] for i in range(0, len(ARGS), 10)]


class _FakeResponse:
    status_code = 200
    headers: dict = {}

    async def aclose(self):
        pass


def _cache():
    cache = MagicMock()
    cache.get_token_limits.return_value = {"maxInputTokens": 200000, "maxOutputTokens": 8192}
    return cache


@pytest.mark.asyncio
async def test_prose_before_a_bracket_call_closes_thinking_first(monkeypatch):
    """The flush that preserves the prose opens a text block, and opening one while
    the thinking block is still open is rejected as an out-of-order content event,
    which aborted the entire response."""
    events = [
        KiroEvent(type="thinking", thinking_content="Pensando. ", is_first_thinking_chunk=True),
        KiroEvent(type="content", content="Vou criar agora.\n"),
        KiroEvent(type="content", content=CALL),
    ]

    async def fake_parse(*args, **kwargs):
        for event in events:
            yield event

    monkeypatch.setattr(streaming_anthropic, "parse_kiro_stream", fake_parse)

    order = []
    async for raw in streaming_anthropic.stream_kiro_to_anthropic(
        _FakeResponse(), "claude-opus-5", _cache(), MagicMock(), request_messages=[], known_input_tokens=10
    ):
        name = None
        for line in raw.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: ") and name:
                payload = json.loads(line[6:])
                if "index" in payload:
                    kind = payload.get("content_block", {}).get("type") or ""
                    order.append((name, payload["index"], kind))

    open_blocks: set = set()
    for name, index, _ in order:
        if name == "content_block_start":
            assert not open_blocks, f"block {index} opened while {open_blocks} was still open"
            open_blocks.add(index)
        elif name == "content_block_stop":
            open_blocks.discard(index)

    kinds = [kind for name, _, kind in order if name == "content_block_start"]
    assert kinds == ["thinking", "text", "tool_use"], "the prose keeps its place before the call"


@pytest.mark.asyncio
async def test_the_non_streaming_response_merges_the_argument_fragments(monkeypatch):
    """stream=false reads back the streamed chunks. Appending each one made every
    fragment its own tool call, nameless and holding a slice of the arguments."""
    frames = [
        {"name": "Write", "toolUseId": "tool-1", "input": ""},
        *[{"input": piece, "toolUseId": "tool-1"} for piece in PIECES],
        {"stop": True, "toolUseId": "tool-1"},
        {"contextUsagePercentage": 10.0},
    ]

    async def fake_parse(*args, **kwargs):
        parser = AwsEventStreamParser()
        for payload in frames:
            async for event in _process_chunk(parser, json.dumps(payload).encode()):
                yield event
        for tool_call in parser.get_unemitted_tool_calls():
            yield KiroEvent(type="tool_use", tool_use=tool_call)

    monkeypatch.setattr(streaming_openai, "parse_kiro_stream", fake_parse)

    result = await streaming_openai.collect_stream_response(
        MagicMock(), _FakeResponse(), "claude-opus-5", _cache(), MagicMock(), request_messages=[]
    )

    calls = result["choices"][0]["message"]["tool_calls"]
    assert len(calls) == 1, "the fragments belong to one call"
    assert calls[0]["function"]["name"] == "Write"
    assert json.loads(calls[0]["function"]["arguments"]) == {
        "file_path": "/tmp/app.py",
        "content": "def main():\n    return 42\n",
    }
