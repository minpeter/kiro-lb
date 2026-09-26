# -*- coding: utf-8 -*-
"""Compare a model's effective context window against one already recorded.

Several models advertise 1000000 maxInputTokens while the runtime endpoint
charges against two thirds of that (see the FALLBACK_MODELS comment in
kiro/config.py). This script sends two English payloads of known cl100k size,
reads the reported contextUsagePercentage for each, and differences them:

    slope = ((p2 - p1) / 100 * advertised) / (n2 - n1)

Two sizes, not one, because the request carries a fixed per-request overhead
that a single sample cannot separate from the payload. Differencing cancels it.

The slope alone does NOT give the window. It is the product of two factors:
how much of the window a token consumes, and how the upstream tokenizer counts
this particular filler relative to cl100k. Measured 2026-09-25, this filler
reads 1.0400 on claude-opus-4.6, whose recorded slope is 0.999 - a ~4% bias
that belongs to the text, not to the model.

So use --reference and read the ratio. Against a model of the same generation
the tokenizer factor cancels and the ratio is the answer. Across generations it
does not cancel: model_costs.py notes that Opus 4.8 counts the same prompt
differently from Opus 4.6, so a cross-generation ratio is not evidence.

Result on 2026-09-25: claude-opus-5.5 and claude-opus-5 both read 1.6400 on
this filler, matching to the fourth decimal, so 5.5 inherits the 666667 that
was measured for 5.

Spends real quota. Run it against a configured account pool, using the runtime
model id (the dotted form; the upstream rejects claude-opus-5-5):

    python measure_context_window.py --model claude-opus-5.5 --reference claude-opus-5
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import uuid
from typing import Any, Optional

import httpx

from kiro import proxy_chain
from kiro.endpoints import KIRO_ENDPOINTS
from kiro.streaming_core import parse_kiro_stream
from kiro.tokenizer import count_tokens
from kiro.utils import get_kiro_headers

ADVERTISED = 1_000_000

# Sizes are far apart so the difference dominates sampling noise, and both stay
# well under the payload guard.
SMALL_TOKENS = 20_000
LARGE_TOKENS = 200_000

# Latin prose, so the cl100k count is the honest one: a CJK filler would make
# the measurement a statement about the tokenizer instead of the window.
_FILLER = (
    "The quick brown fox jumps over the lazy dog while the gateway records "
    "every token it forwards upstream and the operator reads the result. "
)

TIMEOUT = 300.0


def _filler_of(target_tokens: int) -> str:
    """Repeat the filler until it counts target_tokens under cl100k."""
    per_unit = count_tokens(_FILLER, model="claude-haiku-4.5", apply_claude_correction=False)
    if per_unit <= 0:
        raise RuntimeError("tokenizer returned no tokens for the filler")
    return _FILLER * max(1, target_tokens // per_unit)


def _payload(model: str, text: str, profile_arn: Optional[str]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "conversationState": {
            "chatTriggerType": "MANUAL",
            "conversationId": str(uuid.uuid4()),
            "currentMessage": {
                "userInputMessage": {
                    "content": f"{text}\n\nReply with the single word: ok",
                    "modelId": model,
                    "origin": "AI_EDITOR",
                }
            },
            "history": [],
        }
    }
    if profile_arn:
        body["profileArn"] = profile_arn
    return body


async def _sample(client: httpx.AsyncClient, account: Any, model: str, text: str) -> tuple[int, float]:
    """Send one request and return (cl100k tokens sent, reported percentage)."""
    auth = account.auth_manager
    token = await auth.get_access_token()
    headers = get_kiro_headers(auth, token)
    headers.update(KIRO_ENDPOINTS[0].header_overrides())
    profile_arn = getattr(auth, "request_profile_arn", None) or getattr(auth, "profile_arn", None)
    if profile_arn:
        headers["x-amzn-kiro-profile-arn"] = profile_arn

    body = _payload(model, text, profile_arn)
    sent = count_tokens(
        body["conversationState"]["currentMessage"]["userInputMessage"]["content"],
        model="claude-haiku-4.5",
        apply_claude_correction=False,
    )

    url = KIRO_ENDPOINTS[0].url(auth.api_region)
    async with client.stream("POST", url, json=body, headers=headers) as response:
        if response.status_code != 200:
            detail = (await response.aread())[:300].decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {response.status_code}: {detail}")
        percentage: Optional[float] = None
        async for event in parse_kiro_stream(response):
            if event.type == "context_usage" and event.context_usage_percentage:
                percentage = event.context_usage_percentage
    if percentage is None:
        raise RuntimeError("upstream reported no contextUsagePercentage")
    return sent, percentage


async def _slope_for(client: httpx.AsyncClient, account: Any, model: str, small: str, large: str, reps: int) -> float:
    slopes: list[float] = []
    for rep in range(1, reps + 1):
        n1, p1 = await _sample(client, account, model, small)
        n2, p2 = await _sample(client, account, model, large)
        if n2 <= n1 or p2 <= p1:
            print(f"  {model} rep {rep}: unusable ({n1}->{p1}%, {n2}->{p2}%)", file=sys.stderr)
            continue
        slope = ((p2 - p1) / 100 * ADVERTISED) / (n2 - n1)
        slopes.append(slope)
        print(f"  {model} rep {rep}: {n1} tok -> {p1}% | {n2} tok -> {p2}% | slope {slope:.4f}")
    if not slopes:
        raise SystemExit(f"no usable samples for {model}")
    return statistics.median(slopes)


async def measure(model: str, reference: Optional[str], reps: int) -> None:
    from kiro.account_manager import AccountManager

    manager = AccountManager()
    await manager.load_credentials()
    await manager.load_state()
    if not manager._accounts:
        raise SystemExit("no account available")
    for account_id in list(manager._accounts.keys()):
        if await manager._initialize_account(account_id):
            break
    account = await manager.get_next_account(model) or manager.get_first_account()
    if account is None:
        raise SystemExit("no account could be initialized")

    small_text = _filler_of(SMALL_TOKENS)
    large_text = _filler_of(LARGE_TOKENS)

    order = proxy_chain.attempt_order()
    proxy = order[0].url if order else None

    async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT, connect=15.0), proxy=proxy) as client:
        slope = await _slope_for(client, account, model, small_text, large_text, reps)
        reference_slope = (
            await _slope_for(client, account, reference, small_text, large_text, reps) if reference else None
        )

    print(f"\nmodel          {model}")
    print(f"raw slope      {slope:.4f}")
    if reference_slope is None:
        print(f"apparent window {round(ADVERTISED / slope)}")
        print(
            "\nThe raw slope mixes two effects and cannot separate them: the real\n"
            "window, and how the upstream tokenizer counts this filler relative to\n"
            "cl100k. Re-run with --reference <model whose window is already recorded\n"
            "in FALLBACK_MODELS> to get the comparison that does not depend on the\n"
            "tokenizer."
        )
        return

    print(f"reference      {reference} slope {reference_slope:.4f}")
    ratio = slope / reference_slope
    print(f"ratio          {ratio:.4f}")
    if abs(ratio - 1.0) < 0.01:
        print(f"\nverdict        indistinguishable from {reference}: record its recorded window.")
    else:
        print(
            f"\nverdict        {ratio:.4f}x the reference's usage per token, so its window is\n"
            f"               about 1/{ratio:.4f} of {reference}'s - but this only holds if both\n"
            f"               models tokenize alike, which same-generation models do and\n"
            f"               different generations demonstrably do not."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--reference",
        help="a model whose window is already recorded, measured in the same run for comparison",
    )
    parser.add_argument("--reps", type=int, default=2)
    args = parser.parse_args()
    asyncio.run(measure(args.model, args.reference, max(1, args.reps)))


if __name__ == "__main__":
    main()
