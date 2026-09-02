"""A call answered by the gateway itself, and the order a turn was written in."""

import json
from unittest.mock import MagicMock, patch

import pytest

import kiro.streaming_core as streaming_core
import kiro.streaming_openai as streaming_openai
from kiro.streaming_core import KiroEvent, collect_stream_to_result

SEARCH = {
    "id": "toolu_search",
    "type": "function",
    "function": {"name": "web_search", "arguments": '{"query": "python"}'},
}
READ = {
    "id": "toolu_read",
    "type": "function",
    "function": {"name": "Read", "arguments": '{"path": "/tmp/x"}'},
}
SEARCH_ECHO = '[Called web_search with args: {"query": "python"}]'
WRITE_TEXT = '[Called Write with args: {"file_path": "/tmp/a.py", "content": "x"}]'


class _FakeResponse:
    status_code = 200
    headers: dict = {}

    async def aclose(self):
        pass


def _patch(monkeypatch, module, events):
    async def fake_parse(*args, **kwargs):
        for event in events:
            yield event

    monkeypatch.setattr(module, "parse_kiro_stream", fake_parse)


@pytest.mark.asyncio
async def test_an_intercepted_search_is_not_republished_by_its_echo(monkeypatch):
    """web_search is answered with its own results instead of being forwarded, so it
    never lands in the list of emitted calls. Its echo in text therefore matched
    nothing and was kept, handing the client a search call to run again."""
    _patch(
        monkeypatch,
        streaming_openai,
        [
            KiroEvent(type="tool_use", tool_use=SEARCH),
            KiroEvent(type="tool_use", tool_use=READ),
            KiroEvent(type="content", content=SEARCH_ECHO),
        ],
    )
    cache = MagicMock()
    cache.get_token_limits.return_value = {"maxInputTokens": 200000, "maxOutputTokens": 8192}

    async def fake_mcp(*args, **kwargs):
        return "mcp-1", {"results": [{"title": "t", "url": "u", "snippet": "s"}]}

    names = []
    with (
        patch("kiro.mcp_tools.call_kiro_mcp_api", fake_mcp),
        patch("kiro.mcp_tools.generate_search_summary", lambda query, results: "resumo"),
    ):
        async for raw in streaming_openai.stream_kiro_to_openai(
            MagicMock(), _FakeResponse(), "claude-opus-5", cache, MagicMock(), request_messages=[]
        ):
            for line in raw.splitlines():
                if not line.startswith("data: ") or line.strip() == "data: [DONE]":
                    continue
                delta = json.loads(line[6:]).get("choices", [{}])[0].get("delta", {})
                for call in delta.get("tool_calls") or []:
                    name = call.get("function", {}).get("name")
                    if name:
                        names.append(name)

    assert "web_search" not in names, "the gateway already ran the search"
    assert names == ["Read"]


@pytest.mark.asyncio
async def test_a_call_written_before_a_structured_one_keeps_its_place(monkeypatch):
    """Bracket calls were appended after every structured block, so a turn that wrote
    one call and then made another reported them in the wrong order."""
    _patch(
        monkeypatch,
        streaming_core,
        [
            KiroEvent(type="content", content="Primeiro escrevo: " + WRITE_TEXT),
            KiroEvent(type="content", content="\nAgora leio.\n"),
            KiroEvent(type="tool_use", tool_use=READ),
        ],
    )

    result = await collect_stream_to_result(_FakeResponse())

    order = []
    for block in result.content_blocks:
        if block["type"] == "tool_use":
            order.append(block["tool"].get("function", {}).get("name") or block["tool"].get("name"))
        else:
            order.append(block["type"])

    assert order == ["text", "Write", "Read"], "the order the model produced"
