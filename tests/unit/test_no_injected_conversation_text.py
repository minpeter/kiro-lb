"""The gateway must never write into the conversation it proxies.

Earlier revisions synthesized a user turn ("[System Notice] Your previous
response was truncated...") and rewrote tool results ("[API Limitation] ...")
on the next request, then prepended a system section explaining those markers.
None of it came from Kiro: it was gateway-authored text the model read as real
conversation. Truncation is now reported through finish_reason/stop_reason only.
"""

import json
from pathlib import Path

import pytest

from kiro.converters_anthropic import anthropic_to_kiro
from kiro.converters_openai import build_kiro_payload
from kiro.models_anthropic import AnthropicMessagesRequest
from kiro.models_openai import ChatCompletionRequest, ChatMessage

FORBIDDEN = (
    "[System Notice]",
    "[API Limitation]",
    "Output Truncation Handling",
    "(empty placeholder)",
    "Continue",
)


def _openai(messages, **kwargs):
    request = ChatCompletionRequest(
        model="claude-sonnet-4.6",
        messages=[ChatMessage(**message) for message in messages],
        **kwargs,
    )
    return json.dumps(build_kiro_payload(request, "test", "profile"))


def _anthropic(messages, **kwargs):
    request = AnthropicMessagesRequest(
        model="claude-sonnet-4.6", max_tokens=256, messages=messages, **kwargs
    )
    return json.dumps(anthropic_to_kiro(request, "test", "profile"))


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "user", "content": "hello"}],
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "partial answer"}],
        [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}],
    ],
)
def test_openai_payload_is_free_of_gateway_text(messages):
    serialized = _openai(messages)

    for literal in FORBIDDEN:
        assert literal not in serialized


def test_anthropic_payload_is_free_of_gateway_text():
    serialized = _anthropic(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "partial answer"},
        ]
    )

    for literal in FORBIDDEN:
        assert literal not in serialized


def test_recovery_modules_are_gone():
    """The state store and message generators must not come back."""
    for module in ("truncation_recovery", "truncation_state"):
        assert not (Path("kiro") / f"{module}.py").exists()
        with pytest.raises(ModuleNotFoundError):
            __import__(f"kiro.{module}")
