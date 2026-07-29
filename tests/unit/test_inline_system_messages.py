"""Regression tests for clients that inline a system turn in ``messages``.

Kiro rejects unknown roles with ``REQUEST_BODY_INVALID``, so the gateway must
hoist inline system turns into the system prompt instead of forwarding them.
"""

import pytest

from kiro.converters_anthropic import anthropic_to_kiro, split_inline_system_messages
from kiro.models_anthropic import AnthropicMessagesRequest


def _current_content(payload: dict) -> str:
    return payload["conversationState"]["currentMessage"]["userInputMessage"]["content"]


def test_inline_system_message_is_accepted_and_hoisted():
    request = AnthropicMessagesRequest(
        model="claude-sonnet-4.6",
        max_tokens=32,
        messages=[
            {"role": "system", "content": "You are terse."},
            {"role": "user", "content": "hello"},
        ],
    )

    payload = anthropic_to_kiro(request, "test", "profile")
    content = _current_content(payload)

    assert "You are terse." in content
    assert content.rstrip().endswith("hello")
    # No system role may reach the upstream conversation.
    history = payload["conversationState"].get("history", [])
    assert all("userInputMessage" in turn or "assistantResponseMessage" in turn for turn in history)


def test_inline_system_is_merged_after_top_level_system():
    request = AnthropicMessagesRequest(
        model="claude-sonnet-4.6",
        max_tokens=32,
        system="Top level rule.",
        messages=[
            {"role": "system", "content": [{"type": "text", "text": "Inline rule."}]},
            {"role": "user", "content": "hello"},
        ],
    )

    content = _current_content(anthropic_to_kiro(request, "test", "profile"))

    assert content.index("Top level rule.") < content.index("Inline rule.")


def test_split_helper_keeps_conversation_order():
    conversation, system = split_inline_system_messages(
        [
            {"role": "system", "content": "rule"},
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
    )

    assert system == ["rule"]
    assert [message["role"] for message in conversation] == ["user", "assistant"]


def test_system_only_conversation_is_rejected_clearly():
    request = AnthropicMessagesRequest(
        model="claude-sonnet-4.6",
        max_tokens=32,
        messages=[{"role": "system", "content": "only a system turn"}],
    )

    with pytest.raises(ValueError):
        anthropic_to_kiro(request, "test", "profile")
