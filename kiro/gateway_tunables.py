# -*- coding: utf-8 -*-
"""The gateway's dashboard-adjustable scalars, in one place."""

from __future__ import annotations

from kiro.config import ACCOUNT_QUOTA_WEIGHTED_ROUTING, CAPTURE_REQUEST_TEXT, TOKEN_REFRESH_THRESHOLD
from kiro.tunables import Tunable, boolean, bounded_int, one_of

# How long before expiry a token is refreshed. Too low risks racing an expiry
# mid-request; too high refreshes far more often than needed. Bounds keep both
# ends out of the range where the pool misbehaves.
TOKEN_REFRESH_SECONDS = Tunable(
    "token_refresh_seconds",
    default=TOKEN_REFRESH_THRESHOLD,
    coerce=bounded_int(60, 3600),
)

# How the pool orders candidate accounts. Health policy is applied per
# candidate afterwards either way, so this is a preference, not a filter.
#   weighted     - quota-weighted random draw (today's default)
#   sticky       - keep the current cursor, advancing only on success
#   most_credits - deterministic, most remaining quota first
LOAD_BALANCING_STRATEGIES = ("weighted", "sticky", "most_credits")
LOAD_BALANCING = Tunable(
    "load_balancing",
    default="weighted" if ACCOUNT_QUOTA_WEIGHTED_ROUTING else "sticky",
    coerce=one_of(LOAD_BALANCING_STRATEGIES),
)

# Whether the prompt and system prompt are stored with each request log.
# Off by default: it puts conversation text on disk, encrypted but present.
CAPTURE_TEXT = Tunable(
    "capture_request_text",
    default=CAPTURE_REQUEST_TEXT,
    coerce=boolean(),
)

# Caps on generation requests in flight. 0 disables a cap. Holding a burst at
# the door is cheaper than being rate limited and rotating accounts afterwards.
MAX_CONCURRENCY = Tunable("max_concurrency", default=0, coerce=bounded_int(0, 512))
MAX_ACCOUNT_CONCURRENCY = Tunable("max_account_concurrency", default=0, coerce=bounded_int(0, 128))

# How long a queued request waits for a slot before failing with 503.
QUEUE_TIMEOUT_SECONDS = Tunable("queue_timeout_seconds", default=30, coerce=bounded_int(1, 600))

ALL: tuple[Tunable, ...] = (
    TOKEN_REFRESH_SECONDS,
    LOAD_BALANCING,
    CAPTURE_TEXT,
    MAX_CONCURRENCY,
    MAX_ACCOUNT_CONCURRENCY,
    QUEUE_TIMEOUT_SECONDS,
)


def load_all() -> None:
    """Adopt every persisted value. Called once at startup."""
    for tunable in ALL:
        tunable.load()


def reset_all() -> None:
    """Drop every cached value. Used by tests."""
    for tunable in ALL:
        tunable.reset_cache()


def snapshot() -> dict[str, object]:
    return {
        "tokenRefreshSeconds": TOKEN_REFRESH_SECONDS.value(),
        "loadBalancing": LOAD_BALANCING.value(),
        "loadBalancingOptions": list(LOAD_BALANCING_STRATEGIES),
        "captureRequestText": CAPTURE_TEXT.value(),
        "maxConcurrency": MAX_CONCURRENCY.value(),
        "maxAccountConcurrency": MAX_ACCOUNT_CONCURRENCY.value(),
        "queueTimeoutSeconds": QUEUE_TIMEOUT_SECONDS.value(),
    }
