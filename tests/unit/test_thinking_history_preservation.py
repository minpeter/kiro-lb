# -*- coding: utf-8 -*-

"""
Prior-turn reasoning preservation in Kiro history.

Anthropic requires a client to pass `thinking` blocks back with the `tool_use`
they accompanied, so the model can continue the reasoning it paused. Kiro's
history schema has no field for them: measured against
`q.us-east-1.amazonaws.com`, an extra key on `assistantResponseMessage`
(`thinking`, `reasoningContent`) returns HTTP 200 but is silently dropped -- a
passphrase placed only there was recalled 0 times, identical to a run with no
reasoning at all. The same passphrase folded into
`assistantResponseMessage.content` was recalled verbatim.

These tests therefore pin content-folding, and pin that nothing is invented when
the client sends no reasoning: the gateway forwards reasoning it received from
upstream, and never synthesizes it (see tests/unit/test_native_reasoning.py).
"""

from kiro.converters_anthropic import anthropic_to_kiro, convert_anthropic_messages
from kiro.converters_core import build_kiro_history
from kiro.converters_openai import convert_openai_messages_to_unified
from kiro.models_anthropic import AnthropicMessage, AnthropicMessagesRequest
from kiro.models_openai import ChatMessage

REASONING = "The user wants probe.txt. I will call read with path=probe.txt."


def _assistant_history_entry(messages):
    unified = convert_anthropic_messages(messages)
    history = build_kiro_history(unified, "claude-opus-5")
    return next(entry["assistantResponseMessage"] for entry in history if "assistantResponseMessage" in entry)


def test_thinking_block_reaches_kiro_history_as_nested_field():
    """Reasoning the client passed back survives upstream via reasoningContent.reasoningText."""
    messages = [
        AnthropicMessage(role="user", content="Read probe.txt"),
        AnthropicMessage(
            role="assistant",
            content=[
                {"type": "thinking", "thinking": REASONING, "signature": "opaque-sig"},
                {"type": "text", "text": "Reading it now."},
            ],
        ),
        AnthropicMessage(role="user", content="thanks"),
    ]

    entry = _assistant_history_entry(messages)

    assert entry["reasoningContent"] == {"reasoningText": {"text": REASONING, "signature": "opaque-sig"}}
    assert entry["content"] == "Reading it now."


def test_signed_thinking_rides_the_field_not_a_content_tag():
    """Signed reasoning goes into the nested field; content stays clean of any tag."""
    messages = [
        AnthropicMessage(role="user", content="Read probe.txt"),
        AnthropicMessage(
            role="assistant",
            content=[
                {"type": "thinking", "thinking": REASONING, "signature": "opaque-sig"},
                {"type": "text", "text": "Reading it now."},
            ],
        ),
        AnthropicMessage(role="user", content="thanks"),
    ]

    entry = _assistant_history_entry(messages)

    assert "<thinking>" not in entry["content"]
    assert entry["reasoningContent"]["reasoningText"]["text"] == REASONING


def test_reasoning_survives_alongside_tool_uses():
    """The tool-use turn is exactly where Anthropic makes preservation mandatory."""
    messages = [
        AnthropicMessage(role="user", content="Read probe.txt"),
        AnthropicMessage(
            role="assistant",
            content=[
                {"type": "thinking", "thinking": REASONING, "signature": "opaque-sig"},
                {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {"path": "probe.txt"}},
            ],
        ),
        AnthropicMessage(
            role="user",
            content=[{"type": "tool_result", "tool_use_id": "toolu_1", "content": "hello"}],
        ),
    ]

    entry = _assistant_history_entry(messages)

    assert entry["reasoningContent"]["reasoningText"]["text"] == REASONING
    assert entry["toolUses"][0]["toolUseId"] == "toolu_1"


def test_reasoning_reaches_the_full_upstream_payload():
    """End-to-end through anthropic_to_kiro, not just the history helper."""
    request = AnthropicMessagesRequest(
        model="claude-opus-5",
        max_tokens=1024,
        messages=[
            AnthropicMessage(role="user", content="Read probe.txt"),
            AnthropicMessage(
                role="assistant",
                content=[
                    {"type": "thinking", "thinking": REASONING, "signature": "opaque-sig"},
                    {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {"path": "probe.txt"}},
                ],
            ),
            AnthropicMessage(
                role="user",
                content=[{"type": "tool_result", "tool_use_id": "toolu_1", "content": "hello"}],
            ),
        ],
    )

    payload = anthropic_to_kiro(request, "conv-1", "profile")
    history = payload["conversationState"]["history"]
    assistant_entries = [e["assistantResponseMessage"] for e in history if "assistantResponseMessage" in e]

    assert any(
        entry.get("reasoningContent", {}).get("reasoningText", {}).get("text") == REASONING
        for entry in assistant_entries
    )


def test_empty_thinking_block_adds_nothing():
    """An empty thinking field must not inject separators or blank markers."""
    messages = [
        AnthropicMessage(role="user", content="hi"),
        AnthropicMessage(
            role="assistant",
            content=[
                {"type": "thinking", "thinking": "", "signature": "opaque-sig"},
                {"type": "text", "text": "Hello."},
            ],
        ),
        AnthropicMessage(role="user", content="thanks"),
    ]

    assert _assistant_history_entry(messages)["content"] == "Hello."


def test_message_without_reasoning_is_untouched():
    """No reasoning in, no reasoning out: the shape stays exactly as today."""
    messages = [
        AnthropicMessage(role="user", content="hi"),
        AnthropicMessage(role="assistant", content=[{"type": "text", "text": "Hello."}]),
        AnthropicMessage(role="user", content="thanks"),
    ]

    entry = _assistant_history_entry(messages)

    assert entry == {"content": "Hello."}


def test_redacted_thinking_block_is_not_forwarded_as_text():
    """`redacted_thinking` carries opaque encrypted bytes, never model-readable prose."""
    messages = [
        AnthropicMessage(role="user", content="hi"),
        AnthropicMessage(
            role="assistant",
            content=[
                {"type": "redacted_thinking", "data": "AAAAencryptedBytesAAAA"},
                {"type": "text", "text": "Hello."},
            ],
        ),
        AnthropicMessage(role="user", content="thanks"),
    ]

    content = _assistant_history_entry(messages)["content"]

    assert "AAAAencryptedBytesAAAA" not in content
    assert content == "Hello."


def test_openai_reasoning_field_reaches_kiro_history():
    """Parity: `reasoning` is the field the OpenAI serializer emits, so it must round-trip."""
    _, unified = convert_openai_messages_to_unified(
        [
            ChatMessage(role="user", content="Read probe.txt"),
            ChatMessage(role="assistant", content="Reading it now.", reasoning=REASONING),
            ChatMessage(role="user", content="thanks"),
        ]
    )
    history = build_kiro_history(unified, "claude-opus-5")
    entry = next(e["assistantResponseMessage"] for e in history if "assistantResponseMessage" in e)

    assert "reasoningContent" not in entry
    assert "<thinking>" in entry["content"]
    assert REASONING in entry["content"]


def test_openai_legacy_reasoning_content_field_is_accepted():
    """Requests accept both spellings on input, so the legacy one must work too."""
    _, unified = convert_openai_messages_to_unified(
        [
            ChatMessage(role="user", content="Read probe.txt"),
            ChatMessage(role="assistant", content="Reading it now.", reasoning_content=REASONING),
            ChatMessage(role="user", content="thanks"),
        ]
    )
    history = build_kiro_history(unified, "claude-opus-5")
    entry = next(e["assistantResponseMessage"] for e in history if "assistantResponseMessage" in e)

    assert "reasoningContent" not in entry
    assert REASONING in entry["content"]


def test_openai_message_without_reasoning_is_untouched():
    """No reasoning in, no reasoning out on the OpenAI path either."""
    _, unified = convert_openai_messages_to_unified(
        [
            ChatMessage(role="user", content="hi"),
            ChatMessage(role="assistant", content="Hello."),
            ChatMessage(role="user", content="thanks"),
        ]
    )
    history = build_kiro_history(unified, "claude-opus-5")
    entry = next(e["assistantResponseMessage"] for e in history if "assistantResponseMessage" in e)

    assert entry == {"content": "Hello."}


def test_folded_reasoning_is_confined_to_history():
    """The tag must never appear in the turn Kiro is asked to answer.

    Folding puts markup in the conversation, so the risk is the model copying it
    into its reply. Measured against q.us-east-1.amazonaws.com over 6 folded
    trials -- including one that ordered the model to reuse the conversation's
    exact format and one that asked it to show its reasoning first -- the tag was
    reproduced 0 times. What this test pins is the part that is ours: the tag goes
    only into prior turns, never into the current message.
    """
    request = AnthropicMessagesRequest(
        model="claude-opus-5",
        max_tokens=1024,
        messages=[
            AnthropicMessage(role="user", content="Explain caching"),
            AnthropicMessage(
                role="assistant",
                content=[
                    {"type": "thinking", "thinking": REASONING, "signature": "sig"},
                    {"type": "text", "text": "Write-through is authoritative."},
                ],
            ),
            AnthropicMessage(role="user", content="And eviction?"),
        ],
    )

    payload = anthropic_to_kiro(request, "conv-1", "profile")
    current = payload["conversationState"]["currentMessage"]["userInputMessage"]["content"]

    assert "<thinking>" not in current
