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

"""Native Kiro adaptive-thinking request fields.

Kiro exposes reasoning through command-level ``additionalModelRequestFields``.
The schema is strict, and every constraint below was confirmed against
``runtime.us-east-1.kiro.dev``:

* ``thinking.type`` accepts only ``"adaptive"`` or ``"disabled"``. Sending the
  legacy Anthropic ``{"type": "enabled", "budget_tokens": N}`` shape fails with
  ``REQUEST_BODY_INVALID``.
* Numeric budget fields must be at least ``1024``.
* Unknown members of ``additionalModelRequestFields`` are rejected outright, so
  the object is only attached when the client actually asked for reasoning.

Because an invalid object fails the whole request, this module builds the field
set from an allowlist rather than forwarding client input verbatim.
"""

from __future__ import annotations

from typing import Any, Optional

from loguru import logger

#: Models observed to emit native adaptive reasoning frames.
NATIVE_THINKING_MODELS: frozenset[str] = frozenset(
    {
        "claude-opus-4.6",
        "claude-opus-4.7",
        "claude-opus-4.8",
        "claude-opus-5",
        "claude-sonnet-4.6",
    }
)

#: Effort levels accepted by ``output_config.effort``.
SUPPORTED_EFFORTS: frozenset[str] = frozenset({"low", "medium", "high", "xhigh", "max"})

#: Upstream rejects numeric budgets below this value.
MIN_BUDGET_TOKENS = 1024

_DISABLING_VALUES = {"none", "off", "disabled", "0"}


def supports_native_thinking(model_id: str) -> bool:
    return model_id in NATIVE_THINKING_MODELS


def normalize_effort(effort: Optional[str]) -> Optional[str]:
    """Map a client effort level onto an upstream-supported value.

    Returns ``None`` when reasoning should not be requested at all.
    """
    if not effort:
        return None
    value = effort.strip().lower()
    if value in _DISABLING_VALUES:
        return None
    if value in SUPPORTED_EFFORTS:
        return value
    # "minimal" has no upstream equivalent; the lowest real level is "low".
    if value == "minimal":
        return "low"
    logger.debug("Ignoring unsupported reasoning effort: {}", effort)
    return None


def effort_from_anthropic(
    thinking: Optional[dict[str, Any]],
    output_config: Optional[dict[str, Any]],
    max_tokens: Optional[int],
) -> Optional[str]:
    """Derive an effort level from Anthropic-style reasoning parameters.

    Supports both the modern adaptive form and the legacy budget form. The
    legacy form is translated rather than forwarded, because Kiro rejects
    ``thinking.type: "enabled"``.
    """
    if isinstance(output_config, dict):
        effort = normalize_effort(output_config.get("effort"))
        if effort:
            return effort

    if not isinstance(thinking, dict):
        return None

    thinking_type = str(thinking.get("type", "")).strip().lower()
    if thinking_type == "disabled":
        return None
    if thinking_type == "adaptive":
        # Adaptive without an explicit effort: let the model scale itself.
        return "high"
    if thinking_type == "enabled":
        budget = thinking.get("budget_tokens")
        if not isinstance(budget, (int, float)) or budget < MIN_BUDGET_TOKENS:
            return None
        # Translate the requested budget into the closest supported level.
        ceiling = max_tokens if isinstance(max_tokens, int) and max_tokens > 0 else None
        ratio = (budget / ceiling) if ceiling else None
        if ratio is None:
            return "high"
        if ratio >= 0.9:
            return "max"
        if ratio >= 0.7:
            return "xhigh"
        if ratio >= 0.4:
            return "high"
        if ratio >= 0.2:
            return "medium"
        return "low"

    return None


def build_request_fields(model_id: str, effort: Optional[str]) -> Optional[dict[str, Any]]:
    """Build ``additionalModelRequestFields``, or ``None`` to omit it entirely."""
    normalized = normalize_effort(effort)
    if not normalized or not supports_native_thinking(model_id):
        return None
    return {
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {"effort": normalized},
    }


def apply_native_thinking(payload: dict[str, Any], model_id: str, effort: Optional[str]) -> None:
    """Attach native reasoning fields to a Kiro payload when applicable."""
    fields = build_request_fields(model_id, effort)
    if not fields:
        return
    payload["additionalModelRequestFields"] = fields
    logger.debug(
        "Forwarding native adaptive thinking: model={}, effort={}",
        model_id,
        fields["output_config"]["effort"],
    )
