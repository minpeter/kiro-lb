# -*- coding: utf-8 -*-

# kiro-lb
# https://github.com/minpeter/kiro-lb
# Copyright (C) 2026 minpeter
#
# Derived from Kiro Gateway (https://github.com/jwadow/kiro-gateway),
# Copyright (C) 2025 Jwadow.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

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
}

#: Upstream reasons that mean the turn ended early rather than naturally.
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
