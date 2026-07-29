"""Native adaptive-thinking payload contract.

Every constraint asserted here was verified against runtime.us-east-1.kiro.dev:
``thinking.type`` accepts only ``adaptive``/``disabled``, numeric budgets must be
at least 1024, and unknown ``additionalModelRequestFields`` members fail the
whole request with ``REQUEST_BODY_INVALID``.
"""

import pytest

from kiro.converters_anthropic import anthropic_to_kiro
from kiro.converters_openai import build_kiro_payload
from kiro.models_anthropic import AnthropicMessagesRequest
from kiro.models_openai import ChatCompletionRequest, ChatMessage
from kiro.native_thinking import build_request_fields, effort_from_anthropic, normalize_effort

ADAPTIVE = {"type": "adaptive", "display": "summarized"}


def _openai_payload(**kwargs):
    request = ChatCompletionRequest(
        model=kwargs.pop("model", "claude-opus-4.7"),
        messages=[ChatMessage(role="user", content="hi")],
        **kwargs,
    )
    return build_kiro_payload(request, "test", "profile")


def _anthropic_payload(**kwargs):
    request = AnthropicMessagesRequest(
        model=kwargs.pop("model", "claude-opus-4.7"),
        max_tokens=kwargs.pop("max_tokens", 8000),
        messages=[{"role": "user", "content": "hi"}],
        **kwargs,
    )
    return anthropic_to_kiro(request, "test", "profile")


def test_openai_effort_uses_adaptive_shape():
    payload = _openai_payload(reasoning_effort="max")

    assert payload["additionalModelRequestFields"] == {
        "thinking": ADAPTIVE,
        "output_config": {"effort": "max"},
    }


@pytest.mark.parametrize("effort", [None, "none"])
def test_no_reasoning_request_omits_the_field(effort):
    # Attaching an empty or unnecessary object risks REQUEST_BODY_INVALID.
    assert "additionalModelRequestFields" not in _openai_payload(reasoning_effort=effort)


def test_unverified_model_never_receives_the_field():
    assert "additionalModelRequestFields" not in _openai_payload(model="claude-opus-4.5", reasoning_effort="max")
    assert "additionalModelRequestFields" not in _openai_payload(model="glm-5", reasoning_effort="max")


def test_anthropic_adaptive_with_effort_is_forwarded():
    payload = _anthropic_payload(thinking={"type": "adaptive"}, output_config={"effort": "xhigh"})

    assert payload["additionalModelRequestFields"]["output_config"] == {"effort": "xhigh"}
    assert payload["additionalModelRequestFields"]["thinking"] == ADAPTIVE


def test_anthropic_disabled_thinking_omits_the_field():
    assert "additionalModelRequestFields" not in _anthropic_payload(thinking={"type": "disabled"})


def test_legacy_budget_thinking_is_translated_not_forwarded():
    """Upstream rejects thinking.type "enabled", so it must be converted."""
    payload = _anthropic_payload(thinking={"type": "enabled", "budget_tokens": 7600}, max_tokens=8000)

    fields = payload["additionalModelRequestFields"]
    assert fields["thinking"] == ADAPTIVE
    assert fields["output_config"]["effort"] == "max"


def test_budget_below_upstream_minimum_is_ignored():
    # Upstream requires a minimum of 1024, so a smaller budget cannot be honored.
    assert effort_from_anthropic({"type": "enabled", "budget_tokens": 512}, None, 8000) is None


@pytest.mark.parametrize(
    ("budget", "expected"),
    [(8000, "max"), (6000, "xhigh"), (4000, "high"), (2000, "medium"), (1100, "low")],
)
def test_budget_maps_onto_supported_levels(budget, expected):
    assert effort_from_anthropic({"type": "enabled", "budget_tokens": budget}, None, 8000) == expected


def test_minimal_effort_maps_to_lowest_supported_level():
    # Upstream has no "minimal" level.
    assert normalize_effort("minimal") == "low"
    assert normalize_effort("bogus") is None


def test_field_builder_refuses_unsupported_model():
    assert build_request_fields("glm-5", "max") is None
