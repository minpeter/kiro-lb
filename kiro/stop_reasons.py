# -*- coding: utf-8 -*-
"""Mapping of Kiro upstream stop reasons to client protocol values.

The upstream stream reports why generation ended in a metadata frame, for
example ``{"stopReason": "END_TURN"}``. Forwarding that signal matters because a
truncated turn must not be reported to clients as a clean finish: Anthropic
clients decide whether a response was complete from ``stop_reason``.
"""

from __future__ import annotations

from typing import Optional

# Upstream value (normalized to upper case) -> OpenAI finish_reason.
_OPENAI: dict[str, str] = {
    "END_TURN": "stop",
    "STOP_SEQUENCE": "stop",
    "COMPLETE": "stop",
    "MAX_TOKENS": "length",
    "MAX_TOKEN": "length",
    "LENGTH": "length",
    "TOOL_USE": "tool_calls",
    "CONTENT_FILTERED": "content_filter",
    "CONTENT_FILTER": "content_filter",
    "GUARDRAIL_INTERVENED": "content_filter",
    "MODEL_CONTEXT_WINDOW_EXCEEDED": "length",
}

# Upstream value (normalized to upper case) -> Anthropic stop_reason.
_ANTHROPIC: dict[str, str] = {
    "END_TURN": "end_turn",
    "STOP_SEQUENCE": "stop_sequence",
    "COMPLETE": "end_turn",
    "MAX_TOKENS": "max_tokens",
    "MAX_TOKEN": "max_tokens",
    "LENGTH": "max_tokens",
    "TOOL_USE": "tool_use",
    "CONTENT_FILTERED": "refusal",
    "CONTENT_FILTER": "refusal",
    "GUARDRAIL_INTERVENED": "refusal",
    "MODEL_CONTEXT_WINDOW_EXCEEDED": "max_tokens",
}

# Two members of the upstream StopReason enum are deliberately absent from both
# maps: MALFORMED_MODEL_OUTPUT and MALFORMED_TOOL_USE. Neither protocol has a
# value for "the model emitted garbage", and every candidate misleads in its own
# direction - max_tokens sends the client to raise a budget that was never the
# problem, end_turn calls a broken turn complete. An unmapped reason leaves the
# caller's own inference in place, which is the honest option here.

#: Upstream reasons that mean the turn ended early rather than naturally.
#: Membership here is stronger than the maps above: both serializers let it
#: outrank a delivered tool call, so a reason only belongs once it is known to
#: mean the *output* was cut short.
#: GUARDRAIL_INTERVENED is excluded because it ends the turn for content, not
#: length, and already maps to content_filter/refusal.
#: MODEL_CONTEXT_WINDOW_EXCEEDED is excluded because it is not established that
#: it describes cut-off output rather than an oversized input; its mapping
#: already reports the limit, and adding it here would additionally suppress a
#: tool call the model did deliver and invite an auto-continue client to resend
#: an even larger context.
TRUNCATING_REASONS = frozenset({"MAX_TOKENS", "MAX_TOKEN", "LENGTH"})


def normalize(stop_reason: Optional[str]) -> Optional[str]:
    """Return the canonical upper-case upstream reason, if any."""
    if not stop_reason:
        return None
    return stop_reason.strip().upper() or None


def to_openai_finish_reason(stop_reason: Optional[str]) -> Optional[str]:
    """Translate an upstream reason to an OpenAI ``finish_reason``.

    Unknown values return ``None`` so the caller keeps its own inference rather
    than inventing a finish reason the upstream never reported.
    """
    return _OPENAI.get(normalize(stop_reason) or "")


def to_anthropic_stop_reason(stop_reason: Optional[str]) -> Optional[str]:
    """Translate an upstream reason to an Anthropic ``stop_reason``."""
    return _ANTHROPIC.get(normalize(stop_reason) or "")


def is_truncated(stop_reason: Optional[str]) -> bool:
    """Whether the upstream reason indicates the output was cut short."""
    return (normalize(stop_reason) or "") in TRUNCATING_REASONS
