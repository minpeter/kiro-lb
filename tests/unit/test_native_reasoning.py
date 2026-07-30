"""Native Kiro adaptive-thinking contract tests (no prompt-tag fallback)."""

import pytest

from kiro.converters_openai import build_kiro_payload
from kiro.models_openai import ChatCompletionRequest, ChatMessage
from kiro.parsers import AwsEventStreamParser
from kiro.streaming_core import _process_chunk


def test_adaptive_thinking_uses_command_level_upstream_fields():
    request = ChatCompletionRequest(
        model="claude-opus-4.7",
        messages=[ChatMessage(role="user", content="hello")],
        reasoning_effort="max",
    )
    payload = build_kiro_payload(request, "test", "profile")

    assert payload["additionalModelRequestFields"] == {
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {"effort": "max"},
    }
    content = payload["conversationState"]["currentMessage"]["userInputMessage"]["content"]
    assert "<thinking" not in content


def test_unverified_model_does_not_receive_native_fields():
    request = ChatCompletionRequest(
        model="claude-opus-4.5",
        messages=[ChatMessage(role="user", content="hello")],
        reasoning_effort="max",
    )
    payload = build_kiro_payload(request, "test", "profile")
    assert "additionalModelRequestFields" not in payload


def test_native_text_and_signature_frames_are_not_content_heuristics():
    parser = AwsEventStreamParser()
    events = parser.feed(b'{"text":"native reasoning"}{"signature":"upstream-signature"}')

    assert events == [
        {"type": "native_thinking", "data": "native reasoning", "is_first": True},
        {"type": "native_thinking_signature", "data": "upstream-signature"},
    ]


@pytest.mark.asyncio
async def test_native_signature_survives_stream_translation():
    class SignatureParser:
        def feed(self, chunk: bytes):
            return [
                {
                    "type": "native_thinking_signature",
                    "data": "upstream-signature",
                }
            ]

    events = [event async for event in _process_chunk(SignatureParser(), b"signature-frame")]

    assert len(events) == 1
    assert events[0].type == "thinking_signature"
    assert events[0].thinking_signature == "upstream-signature"
