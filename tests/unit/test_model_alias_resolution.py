"""Advertised model aliases must resolve to real upstream identifiers.

`/v1/models` advertises `auto-kiro` (an alias that avoids colliding with
Cursor's own "auto" model). The converters previously consulted only
HIDDEN_MODELS, so the alias was forwarded verbatim and upstream answered
`INVALID_MODEL_ID` — every advertised `auto-kiro` request failed while plain
`auto` worked.
"""

import pytest

from kiro.config import HIDDEN_MODELS, MODEL_ALIASES
from kiro.converters_anthropic import anthropic_to_kiro
from kiro.converters_openai import build_kiro_payload
from kiro.model_resolver import get_model_id_for_kiro
from kiro.models_anthropic import AnthropicMessagesRequest
from kiro.models_openai import ChatCompletionRequest, ChatMessage


def _openai_model_id(model: str) -> str:
    request = ChatCompletionRequest(model=model, messages=[ChatMessage(role="user", content="hi")])
    payload = build_kiro_payload(request, "test", "profile")
    return payload["conversationState"]["currentMessage"]["userInputMessage"]["modelId"]


def _anthropic_model_id(model: str) -> str:
    request = AnthropicMessagesRequest(
        model=model, max_tokens=32, messages=[{"role": "user", "content": "hi"}]
    )
    payload = anthropic_to_kiro(request, "test", "profile")
    return payload["conversationState"]["currentMessage"]["userInputMessage"]["modelId"]


def test_every_advertised_alias_maps_to_a_real_model():
    assert MODEL_ALIASES, "the alias table should not be empty"
    for alias, target in MODEL_ALIASES.items():
        assert get_model_id_for_kiro(alias, HIDDEN_MODELS, MODEL_ALIASES) == target


@pytest.mark.parametrize("build", [_openai_model_id, _anthropic_model_id])
def test_alias_is_translated_on_both_protocols(build):
    assert build("auto-kiro") == "auto"


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("auto", "auto"),
        ("claude-opus-4.7", "claude-opus-4.7"),
        ("claude-haiku-4-5-20251001", "claude-haiku-4.5"),
    ],
)
def test_non_alias_models_are_unchanged(requested, expected):
    assert _openai_model_id(requested) == expected
