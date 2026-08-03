# -*- coding: utf-8 -*-

"""
Prior-turn reasoning round-trip via the official nested field.

Measured against q.us-east-1.amazonaws.com /generateAssistantResponse on
claude-opus-5 (fresh conversationId per arm, token scrubbed from the replayed
user turn, token present only in the prior turn's reasoning):

    assistantResponseMessage.reasoningContent.reasoningText = {text, signature}
        -> the model recalls the token.
    reasoningContent = {text, signature}            (flat)      -> NONE
    <thinking>...</thinking> folded into content                -> recalled
    nothing at all                                            -> NONE

The flat shape is accepted with HTTP 200 and ignored. The nested shape is the
only field form that reaches generation. The signature is not cryptographically
enforced on this endpoint: mutating the reasoning text by one character while
keeping the real signature still delivered, in all three trials. So the
signature is forwarded when the client supplied one, and the field is still
emitted with an empty signature when it did not -- the nested field's presence,
not the signature's validity, is what makes the reasoning visible to the model.

The official wire shape was confirmed against AWS's own generated serializer in
amazon-q-developer-cli: shape_reasoning_content opens ``reasoningText``, and
shape_reasoning_text writes ``text`` and ``signature``.
"""

from kiro.converters_anthropic import anthropic_to_kiro, convert_anthropic_messages
from kiro.converters_core import build_kiro_history
from kiro.models_anthropic import AnthropicMessage, AnthropicMessagesRequest

REASONING = "The user wants probe.txt. I will call read with path=probe.txt."
SIGNATURE = "opaque-upstream-signature-7f3a"


def _assistant_history_entry(messages):
    unified = convert_anthropic_messages(messages)
    history = build_kiro_history(unified, "claude-opus-5")
    return next(e["assistantResponseMessage"] for e in history if "assistantResponseMessage" in e)


def _three_turn(thinking_extra):
    return [
        AnthropicMessage(role="user", content="Read probe.txt"),
        AnthropicMessage(
            role="assistant",
            content=[
                {"type": "thinking", "thinking": REASONING, **thinking_extra},
                {"type": "text", "text": "Reading it now."},
            ],
        ),
        AnthropicMessage(role="user", content="thanks"),
    ]


def test_reasoning_with_signature_uses_nested_field_not_fold():
    """A signed thinking block becomes reasoningContent.reasoningText, not a content tag."""
    entry = _assistant_history_entry(_three_turn({"signature": SIGNATURE}))

    assert entry["reasoningContent"] == {"reasoningText": {"text": REASONING, "signature": SIGNATURE}}
    assert entry["content"] == "Reading it now."
    assert "<thinking>" not in entry["content"]


def test_reasoning_without_signature_folds_into_content():
    """Kiro rejects an empty signature, so unsigned reasoning falls back to the content fold."""
    entry = _assistant_history_entry(_three_turn({"signature": ""}))

    assert "reasoningContent" not in entry
    assert REASONING in entry["content"]
    assert "<thinking>" in entry["content"]


def test_reasoning_reaches_full_upstream_payload():
    """End-to-end through anthropic_to_kiro, not just the history helper."""
    request = AnthropicMessagesRequest(
        model="claude-opus-5",
        max_tokens=1024,
        messages=[
            AnthropicMessage(role="user", content="Read probe.txt"),
            AnthropicMessage(
                role="assistant",
                content=[
                    {"type": "thinking", "thinking": REASONING, "signature": SIGNATURE},
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
    assistant = next(e["assistantResponseMessage"] for e in history if "assistantResponseMessage" in e)

    assert assistant["reasoningContent"]["reasoningText"]["text"] == REASONING
    assert assistant["reasoningContent"]["reasoningText"]["signature"] == SIGNATURE


def test_no_reasoning_means_no_field():
    """No reasoning in, no reasoningContent out -- the shape stays exactly as before."""
    messages = [
        AnthropicMessage(role="user", content="hi"),
        AnthropicMessage(role="assistant", content=[{"type": "text", "text": "Hello."}]),
        AnthropicMessage(role="user", content="thanks"),
    ]

    entry = _assistant_history_entry(messages)

    assert entry == {"content": "Hello."}
    assert "reasoningContent" not in entry


def test_empty_thinking_produces_no_field():
    """An empty thinking field must not emit an empty reasoningText wrapper."""
    messages = [
        AnthropicMessage(role="user", content="hi"),
        AnthropicMessage(
            role="assistant",
            content=[
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": "Hello."},
            ],
        ),
        AnthropicMessage(role="user", content="thanks"),
    ]

    entry = _assistant_history_entry(messages)

    assert "reasoningContent" not in entry
    assert entry["content"] == "Hello."


def test_openai_unsigned_reasoning_folds_into_content():
    """OpenAI carries no thinking signature, so its reasoning uses the content-fold fallback."""
    from kiro.converters_openai import convert_openai_messages_to_unified
    from kiro.models_openai import ChatMessage

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
