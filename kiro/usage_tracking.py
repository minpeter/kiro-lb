# -*- coding: utf-8 -*-
"""Token accounting per key, account, and model.

Both facts a finished request has to be attributed to are known long before the
token counts are: the key at authentication, the account at selection. The counts
are only final deep inside the serializers, so both identities travel in
ContextVars instead of through four call signatures. Counts accumulate in memory
and are flushed to the dashboard store in batches, keeping the data path free of a
write per request while still surviving a restart.

The account is recorded at the same grain as the key rather than in a separate
counter, because "which account served these tokens" is a property of the same
event: splitting it out is what left the store unable to answer it at all. A
request that fails over across several accounts attributes its tokens to the one
that actually produced the response - the earlier attempts produced none.
"""

from __future__ import annotations

import threading
import time
from contextvars import ContextVar
from typing import Dict, List, Tuple

from loguru import logger

from kiro.model_resolver import normalize_model_name

# Identity of the key that authenticated the current request. ROOT_KEY_ID marks
# the legacy environment key, which has no dashboard-managed row.
ROOT_KEY_ID = "root"

current_api_key_id: ContextVar[str | None] = ContextVar("current_api_key_id", default=None)

# Account that produced the current response. Set when an attempt is about to be
# made and overwritten on failover, so at completion it names the account that
# actually served rather than the first one tried. UNKNOWN_ACCOUNT_ID covers the
# legacy single-account mode and any path that records tokens without going
# through selection: the tokens are real and must not be dropped, but they cannot
# be attributed to an account.
UNKNOWN_ACCOUNT_ID = "unknown"

current_account_id: ContextVar[str | None] = ContextVar("current_account_id", default=None)

# (key_id, account_id, model) -> [prompt_tokens, completion_tokens, requests,
#                                 generation_ms, timed_completion_tokens]
_pending: Dict[Tuple[str, str, str], List[int]] = {}
_lock = threading.Lock()


def record_token_usage(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    generation_seconds: float | None = None,
) -> None:
    """Attribute a finished request's tokens to the key that made it.

    The model name is normalized first. Callers pass whatever the client sent,
    and `claude-sonnet-4-5`, `claude-sonnet-4.5` and
    `claude-sonnet-4-5-20251001` are one model: storing them separately splits a
    single model's totals across rows that no consumer can rejoin.

    `generation_seconds` is how long the upstream took to produce this response,
    measured by the caller because only it knows when the stream actually
    finished. The request log cannot supply it: its latency is recorded when the
    handler returns, which for a streaming response is first-byte time, not
    generation time.

    Output tokens are counted twice: once as the real total, and once restricted
    to requests that were also timed. Throughput has to divide the second by the
    duration, because the two are otherwise gathered over different request sets -
    rows predating this column hold tokens with no time at all, and dividing the
    full total by a partial duration produced 82,752 tok/s on the live store.
    """
    key_id = current_api_key_id.get()
    if key_id is None or not model:
        return
    # An unattributed account is recorded as UNKNOWN_ACCOUNT_ID rather than
    # dropped. The tokens were really spent, and losing them would make the
    # per-account totals silently disagree with the per-key ones.
    account_id = current_account_id.get() or UNKNOWN_ACCOUNT_ID
    model = normalize_model_name(model) or model
    completion = max(0, int(completion_tokens or 0))
    timed = generation_seconds is not None and generation_seconds > 0
    try:
        with _lock:
            entry = _pending.setdefault((key_id, account_id, model), [0, 0, 0, 0, 0])
            entry[0] += max(0, int(prompt_tokens or 0))
            entry[1] += completion
            entry[2] += 1
            if timed:
                # Milliseconds keep the accumulator an int, matching the token
                # counters and the SQLite columns.
                entry[3] += int((generation_seconds or 0) * 1000)
                entry[4] += completion
    except Exception as exc:
        # Accounting must never break the proxy data plane.
        logger.debug("Token usage accounting skipped: {}", exc)


class GenerationTimer:
    """Measure how long an upstream response took to produce.

    A plain `time.perf_counter()` pair would do, except that a streaming
    generator can be abandoned mid-flight; keeping the start in an object makes
    the elapsed value available wherever the finally-block ends up.
    """

    __slots__ = ("_started",)

    def __init__(self) -> None:
        self._started = time.perf_counter()

    @property
    def elapsed(self) -> float:
        return max(0.0, time.perf_counter() - self._started)


def drain_pending_usage() -> List[Tuple[str, str, str, int, int, int, int, int]]:
    with _lock:
        drained = [
            (key_id, account_id, model, counts[0], counts[1], counts[2], counts[3], counts[4])
            for (key_id, account_id, model), counts in _pending.items()
        ]
        _pending.clear()
    return drained


def restore_pending_usage(rows: List[Tuple[str, str, str, int, int, int, int, int]]) -> None:
    """Add a failed durable flush back without overwriting concurrent usage."""
    with _lock:
        for key_id, account_id, model, prompt, completion, requests, generation_ms, timed in rows:
            entry = _pending.setdefault((key_id, account_id, model), [0, 0, 0, 0, 0])
            for index, value in enumerate((prompt, completion, requests, generation_ms, timed)):
                entry[index] += value
