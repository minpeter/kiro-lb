"""Stream fidelity tests: verbatim deltas and upstream stop reasons.

Both behaviors were observed as silent data problems in other Kiro proxies:
repeated deltas were dropped as "replays", and the upstream stop reason was
ignored so a truncated turn looked like a clean finish.
"""

import json

import httpx
import pytest

from kiro.parsers import AwsEventStreamParser
from kiro.stop_reasons import (
    is_truncated,
    normalize,
    to_anthropic_stop_reason,
    to_openai_finish_reason,
)
from kiro.streaming_core import parse_kiro_stream


def _frames(*contents: str) -> bytes:
    return "".join('{"content":"%s"}' % chunk for chunk in contents).encode()


@pytest.mark.parametrize(
    ("deltas", "expected"),
    [
        (("666", "666", "666", "6"), "6666666666"),
        (("ab", "ab", "ab", "ab"), "abababab"),
        (("1", "833"), "1833"),
        (("20", "12"), "2012"),
        (("runtime", "-raw-capture-auto"), "runtime-raw-capture-auto"),
    ],
)
def test_repeating_deltas_are_forwarded_verbatim(deltas, expected):
    """Kiro sends incremental deltas, so identical frames are real output."""
    parser = AwsEventStreamParser()

    events = parser.feed(_frames(*deltas))

    assert "".join(event["data"] for event in events) == expected


def test_stop_reason_frame_is_surfaced():
    parser = AwsEventStreamParser()

    events = parser.feed(b'{"content":"hi"}{"stopReason":"END_TURN"}')

    assert events[-1] == {"type": "stop_reason", "data": "END_TURN"}


def test_blank_stop_reason_is_ignored():
    parser = AwsEventStreamParser()

    assert parser.feed(b'{"stopReason":""}') == []


@pytest.mark.parametrize(
    ("upstream", "openai", "anthropic"),
    [
        ("END_TURN", "stop", "end_turn"),
        ("MAX_TOKENS", "length", "max_tokens"),
        ("TOOL_USE", "tool_calls", "tool_use"),
        ("CONTENT_FILTERED", "content_filter", "refusal"),
        ("end_turn", "stop", "end_turn"),
    ],
)
def test_known_reasons_map_to_both_protocols(upstream, openai, anthropic):
    assert to_openai_finish_reason(upstream) == openai
    assert to_anthropic_stop_reason(upstream) == anthropic


def test_unknown_reason_does_not_invent_a_value():
    # Callers must keep their own inference rather than fabricate a reason.
    assert to_openai_finish_reason("SOMETHING_NEW") is None
    assert to_anthropic_stop_reason("SOMETHING_NEW") is None
    assert normalize(None) is None


def test_truncation_is_detected_from_upstream_reason():
    assert is_truncated("MAX_TOKENS")
    assert not is_truncated("END_TURN")
    assert not is_truncated(None)


@pytest.mark.asyncio
async def test_tool_arguments_reach_the_client_as_they_arrive():
    """Regression: the parser accumulated every argument fragment and emitted
    nothing until the stop frame, so a whole tool call reached the client in one
    input_json_delta. Clients render a tool's arguments from those fragments, so
    collapsing them removed the progressive view of what the tool was about to
    do. Each upstream fragment must produce one delta."""
    fragments = ['{"file_path":', '"/tmp/a.py",', '"content":', '"print(1)"}']
    frames = [b'{"name":"Write","toolUseId":"tool-9"}']
    frames += [b'{"input":' + json.dumps(part).encode() + b"}" for part in fragments]
    frames += [b'{"stop":true}']

    events = [event async for event in parse_kiro_stream(httpx.Response(200, content=b"".join(frames)))]

    deltas = [event for event in events if event.type == "tool_use_delta"]
    assert len(deltas) == len(fragments), "one delta per upstream fragment"

    streamed = "".join(event.tool_input_delta or "" for event in events if event.tool_input_delta)
    assert streamed == "".join(fragments)
    assert json.loads(streamed) == {"file_path": "/tmp/a.py", "content": "print(1)"}

    completed = [event for event in events if event.type == "tool_use"]
    assert len(completed) == 1, "the call is still completed exactly once"


@pytest.mark.asyncio
async def test_a_single_frame_tool_call_still_completes():
    """A call whose arguments arrive whole, in the frame that also stops it, has
    no fragments to stream and must not regress into a missing block."""
    response = httpx.Response(
        200,
        content=(b'{"name":"read","toolUseId":"tool-1","input":"{\\"path\\":\\"a\\"}","stop":true}'),
    )

    events = [event async for event in parse_kiro_stream(response)]

    assert [event.type for event in events] == ["tool_use"]
    assert events[0].tool_use["function"]["arguments"] == '{"path": "a"}'


@pytest.mark.asyncio
async def test_native_tool_use_precedes_terminal_stop_reason():
    response = httpx.Response(
        200,
        content=(
            b'{"name":"read","toolUseId":"tool-1"}'
            b'{"input":"{\\"path\\":\\"README.md\\"}"}'
            b'{"stop":true}'
            b'{"stopReason":"TOOL_USE"}'
        ),
    )

    events = [event async for event in parse_kiro_stream(response)]

    # The arguments are streamed as they arrive, so the call is announced, filled
    # in fragment by fragment, and only then completed. The invariant the name
    # refers to still holds: the completed call precedes the terminal stop reason.
    assert [event.type for event in events] == [
        "tool_use_start",
        "tool_use_delta",
        "tool_use",
        "stop_reason",
    ]
    assert events[0].tool_use_id == "tool-1"
    assert events[0].tool_use_name == "read"
    # Concatenating the fragments must reproduce the upstream JSON verbatim: the
    # client assembles them by appending, so a re-serialized fragment would
    # corrupt the result.
    fragments = "".join(event.tool_input_delta or "" for event in events[:2])
    assert fragments == '{"path":"README.md"}'
    assert events[2].tool_use == {
        "id": "tool-1",
        "type": "function",
        "function": {
            "name": "read",
            "arguments": '{"path": "README.md"}',
        },
    }
