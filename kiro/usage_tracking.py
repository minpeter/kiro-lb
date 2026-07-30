# -*- coding: utf-8 -*-
"""Per-key, per-model token accounting for the dashboard.

The calling key is known at authentication time but the token counts are only
final deep inside the serializers, so the identity travels in a ContextVar
instead of through four call signatures. Counts accumulate in memory and are
flushed to the dashboard store in batches, keeping the data path free of a
write per request while still surviving a restart.
"""

from __future__ import annotations

import threading
from contextvars import ContextVar
from typing import Dict, List, Tuple

from loguru import logger

# Identity of the key that authenticated the current request. ROOT_KEY_ID marks
# the legacy environment key, which has no dashboard-managed row.
ROOT_KEY_ID = "root"

current_api_key_id: ContextVar[str | None] = ContextVar("current_api_key_id", default=None)

# (key_id, model) -> [prompt_tokens, completion_tokens, requests]
_pending: Dict[Tuple[str, str], List[int]] = {}
_lock = threading.Lock()


def record_token_usage(model: str, prompt_tokens: int, completion_tokens: int) -> None:
    """Attribute a finished request's tokens to the key that made it."""
    key_id = current_api_key_id.get()
    if key_id is None or not model:
        return
    try:
        with _lock:
            entry = _pending.setdefault((key_id, model), [0, 0, 0])
            entry[0] += max(0, int(prompt_tokens or 0))
            entry[1] += max(0, int(completion_tokens or 0))
            entry[2] += 1
    except Exception as exc:
        # Accounting must never break the proxy data plane.
        logger.debug("Token usage accounting skipped: {}", exc)


def drain_pending_usage() -> List[Tuple[str, str, int, int, int]]:
    with _lock:
        drained = [(key_id, model, counts[0], counts[1], counts[2]) for (key_id, model), counts in _pending.items()]
        _pending.clear()
    return drained
