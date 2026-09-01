# -*- coding: utf-8 -*-
"""Kiro credit multipliers and context windows, from the published model table.

Kiro bills in credits, not tokens. Each model carries a multiplier relative to
``auto`` at 1.0x: a task costing 10 credits on auto costs 22 on Opus and 0.5 on
Qwen3 Coder Next.

The estimate this module produces is deliberately coarse. Kiro's own
documentation warns that models sharing a multiplier do not consume the same
credits per task, because consumption depends on generated tokens, internal
thinking depth, and tokenizer differences - Opus 4.8 counts the same prompt
differently from Opus 4.6. Higher reasoning effort also spends more.

So treat the value as a relative indicator for comparing requests, not as the
number that will appear on a bill. ``credits_for`` returns None for an unknown
model rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from kiro.model_resolver import normalize_model_name


@dataclass(frozen=True)
class ModelCost:
    """Published multiplier and context window for one model."""

    multiplier: float
    context_tokens: int


BASELINE_MODEL = "auto"

# Keys are normalized model ids. Source: Kiro's model comparison table.
MODEL_COSTS: dict[str, ModelCost] = {
    "gpt-5.6-sol": ModelCost(2.4, 272_000),
    "gpt-5.6-terra": ModelCost(1.0, 272_000),
    "gpt-5.6-luna": ModelCost(0.1, 272_000),
    "claude-opus-5": ModelCost(2.2, 1_000_000),
    "claude-opus-4.8": ModelCost(2.2, 1_000_000),
    "claude-opus-4.7": ModelCost(2.2, 1_000_000),
    "claude-opus-4.6": ModelCost(2.2, 1_000_000),
    "claude-opus-4.5": ModelCost(2.2, 200_000),
    "claude-sonnet-5": ModelCost(1.3, 1_000_000),
    "claude-sonnet-4.6": ModelCost(1.3, 1_000_000),
    "claude-sonnet-4.5": ModelCost(1.3, 200_000),
    "claude-sonnet-4": ModelCost(1.3, 200_000),
    "auto": ModelCost(1.0, 0),
    "claude-haiku-4.5": ModelCost(0.4, 200_000),
    "deepseek-3.2": ModelCost(0.25, 128_000),
    "minimax-m2.5": ModelCost(0.25, 200_000),
    "glm-5": ModelCost(0.5, 200_000),
    "minimax-m2.1": ModelCost(0.15, 200_000),
    "qwen3-coder-next": ModelCost(0.05, 256_000),
}


def cost_for(model: Optional[str]) -> Optional[ModelCost]:
    """Return the published cost entry for a model, or None when unknown."""
    if not model:
        return None
    direct = MODEL_COSTS.get(model.strip().lower())
    if direct is not None:
        return direct
    try:
        normalized = normalize_model_name(model)
    except Exception:
        return None
    return MODEL_COSTS.get((normalized or "").strip().lower())


def multiplier_for(model: Optional[str]) -> Optional[float]:
    entry = cost_for(model)
    return entry.multiplier if entry else None


def credits_for(model: Optional[str], baseline_credits: float) -> Optional[float]:
    """Scale a baseline credit figure by the model's multiplier.

    ``baseline_credits`` is what the same task would cost on ``auto``. Returns
    None when the model is unknown, so a caller shows nothing rather than a
    fabricated number.
    """
    multiplier = multiplier_for(model)
    if multiplier is None:
        return None
    return baseline_credits * multiplier


def table() -> list[dict[str, object]]:
    """The full table, for the dashboard to render."""
    return [
        {
            "model": model,
            "multiplier": entry.multiplier,
            "contextTokens": entry.context_tokens or None,
        }
        for model, entry in sorted(MODEL_COSTS.items(), key=lambda item: -item[1].multiplier)
    ]
