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

Some models are billed in two tiers. The GPT-5.6 family charges double above
272k tokens, so its entries carry a second rate and the threshold it starts
above, and ``credits_for`` picks the tier from the token count it is given.
Encoding a published boundary sharpens the estimate; it does not turn it into a
receipt.

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
    """Published multipliers and context window for one model.

    ``multiplier`` is the short-context rate, the only rate most models have.
    The GPT-5.6 family is billed in two tiers: requests above 272k tokens cost
    double, so those entries also carry ``long_multiplier`` and the
    ``long_threshold`` the higher rate starts above. Both stay None for a
    single-rate model, and no tier is inferred for a model that does not publish
    one - a guessed "twice the short rate" would read as a published figure.
    """

    multiplier: float
    context_tokens: int
    long_multiplier: Optional[float] = None
    long_threshold: Optional[int] = None

    def multiplier_at(self, input_tokens: Optional[int] = None) -> float:
        """The rate that applies to a request of this size.

        A caller that does not know the token count gets the short rate rather
        than nothing: it is the tier every request under the threshold lands in,
        and the alternative is a table with no number in it at all.
        """
        if self.long_multiplier is None or self.long_threshold is None:
            return self.multiplier
        if input_tokens is None or input_tokens <= self.long_threshold:
            return self.multiplier
        return self.long_multiplier


BASELINE_MODEL = "auto"

# Token count above which the GPT-5.6 family switches to its long-context rate.
GPT_5_6_LONG_THRESHOLD = 272_000

# Keys are normalized model ids. Source: Kiro's model comparison table.
MODEL_COSTS: dict[str, ModelCost] = {
    "gpt-5.6-sol": ModelCost(4.4, 1_000_000, 8.8, GPT_5_6_LONG_THRESHOLD),
    "gpt-5.6-terra": ModelCost(2.2, 1_000_000, 4.4, GPT_5_6_LONG_THRESHOLD),
    "gpt-5.6-luna": ModelCost(1.1, 1_000_000, 2.2, GPT_5_6_LONG_THRESHOLD),
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


def multiplier_for(model: Optional[str], input_tokens: Optional[int] = None) -> Optional[float]:
    """The rate for a model, at the tier ``input_tokens`` falls in when given."""
    entry = cost_for(model)
    return entry.multiplier_at(input_tokens) if entry else None


def credits_for(
    model: Optional[str],
    baseline_credits: float,
    input_tokens: Optional[int] = None,
) -> Optional[float]:
    """Scale a baseline credit figure by the model's multiplier.

    ``baseline_credits`` is what the same task would cost on ``auto``. Returns
    None when the model is unknown, so a caller shows nothing rather than a
    fabricated number.

    ``input_tokens`` selects the tier for a two-tier model. Omitting it bills at
    the short rate, which understates a request that crossed the threshold: pass
    the count wherever it is known.
    """
    multiplier = multiplier_for(model, input_tokens)
    if multiplier is None:
        return None
    return baseline_credits * multiplier


def table() -> list[dict[str, object]]:
    """The full table, for the dashboard to render.

    Two-tier models carry both rates and the threshold; the long fields are null
    for every single-rate model, so a consumer can tell "no second tier" from a
    second tier that happens to match the first.
    """
    return [
        {
            "model": model,
            "multiplier": entry.multiplier,
            "contextTokens": entry.context_tokens or None,
            "longMultiplier": entry.long_multiplier,
            "longThresholdTokens": entry.long_threshold,
        }
        for model, entry in sorted(MODEL_COSTS.items(), key=lambda item: -item[1].multiplier)
    ]
