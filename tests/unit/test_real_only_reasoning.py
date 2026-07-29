"""Regression tests for kiro-lb's real-upstream-only reasoning policy."""

from kiro.converters_openai import build_kiro_payload
from kiro.models_openai import ChatCompletionRequest, ChatMessage


def test_reasoning_effort_is_not_translated_to_prompt_tags():
    """Compatibility fields may be accepted, but they must never alter prompts."""
    request = ChatCompletionRequest(
        model="claude-opus-4.7",
        messages=[ChatMessage(role="user", content="hello")],
        reasoning_effort="xhigh",
    )

    payload = build_kiro_payload(request, "test", "profile")
    content = payload["conversationState"]["currentMessage"]["userInputMessage"]["content"]

    assert "<thinking" not in content
    assert "<reasoning" not in content
    assert "<max_thinking_length>" not in content
    assert content.endswith("hello")
