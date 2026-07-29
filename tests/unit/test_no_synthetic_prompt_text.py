"""The gateway must never inject readable filler into the conversation.

Kiro accepts empty content in every payload position (verified by live probe),
so synthetic turns carry no text. Any literal placeholder is read by the model
as a real user instruction: with "(empty placeholder)" an assistant-prefill
request produced "Looks like your message came through empty" instead of
continuing the partial answer.
"""

import json

import pytest

from kiro.converters_core import (
    UnifiedMessage,
    build_kiro_history,
    ensure_alternating_roles,
    ensure_first_message_is_user,
)
from kiro.converters_openai import build_kiro_payload
from kiro.models_openai import ChatCompletionRequest, ChatMessage

FORBIDDEN = ("(empty placeholder)", "(empty)", "Continue")


def _payload(messages):
    request = ChatCompletionRequest(
        model="claude-sonnet-4.6",
        messages=[ChatMessage(**message) for message in messages],
    )
    return build_kiro_payload(request, "test", "profile")


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "user", "content": "Say hi"}, {"role": "assistant", "content": "Hi there."}],
        [{"role": "assistant", "content": "Hello"}, {"role": "user", "content": "hi"}],
        [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}],
        [{"role": "user", "content": ""}],
    ],
)
def test_payload_contains_no_synthetic_instructions(messages):
    serialized = json.dumps(_payload(messages))

    for literal in FORBIDDEN:
        assert literal not in serialized


def test_assistant_prefill_sends_empty_current_turn():
    """An assistant-last request means "continue", not a new user message."""
    payload = _payload(
        [{"role": "user", "content": "Name the first six primes."},
         {"role": "assistant", "content": "The first six primes are 2, 3, 5,"}]
    )

    state = payload["conversationState"]
    assert state["currentMessage"]["userInputMessage"]["content"] == ""
    # The partial answer must be preserved as history so the model can continue.
    assert state["history"][-1]["assistantResponseMessage"]["content"].endswith("2, 3, 5,")


def test_synthetic_turns_are_textless():
    first = ensure_first_message_is_user([UnifiedMessage(role="assistant", content="Hello")])
    assert first[0].role == "user"
    assert first[0].content == ""

    alternated = ensure_alternating_roles(
        [UnifiedMessage(role="user", content="a"), UnifiedMessage(role="user", content="b")]
    )
    assert [message.role for message in alternated] == ["user", "assistant", "user"]
    assert alternated[1].content == ""


def test_history_keeps_empty_turns_without_filler():
    history = build_kiro_history(
        [UnifiedMessage(role="user", content=""), UnifiedMessage(role="assistant", content="")],
        "claude-sonnet-4.6",
    )

    assert history[0]["userInputMessage"]["content"] == ""
    assert history[1]["assistantResponseMessage"]["content"] == ""
