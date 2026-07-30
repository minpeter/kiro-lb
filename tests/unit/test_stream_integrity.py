"""Stream fidelity tests: verbatim deltas and upstream stop reasons.

Both behaviors were observed as silent data problems in other Kiro proxies:
repeated deltas were dropped as "replays", and the upstream stop reason was
ignored so a truncated turn looked like a clean finish.
"""

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

    assert [event.type for event in events] == ["tool_use", "stop_reason"]
    assert events[0].tool_use == {
        "id": "tool-1",
        "type": "function",
        "function": {
            "name": "read",
            "arguments": '{"path": "README.md"}',
        },
    }
