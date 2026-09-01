# -*- coding: utf-8 -*-
"""Caps how many generation requests run at once, with a bounded wait.

Without a cap, a burst from the client goes straight upstream and comes back as
429s, which cost an account cooldown each. Holding requests at the door is
cheaper than being rate limited and rotating afterwards.

Two limits apply, both optional and both off by default:
  global    - total in-flight generation requests
  account   - in-flight requests per account

A request over the limit waits up to ``queue_timeout`` for a slot. On timeout it
fails with 503 rather than queueing forever, so a stuck upstream surfaces as an
error instead of a hang.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import AsyncIterator, Optional

from loguru import logger

from kiro.gateway_tunables import MAX_ACCOUNT_CONCURRENCY, MAX_CONCURRENCY, QUEUE_TIMEOUT_SECONDS


class QueueTimeout(RuntimeError):
    """Raised when no slot became free within the configured wait."""


class _Gate:
    """A semaphore that is rebuilt when its limit changes."""

    def __init__(self) -> None:
        self._limit = 0
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._waiting = 0
        self._held = 0

    def _resolve(self, limit: int) -> Optional[asyncio.Semaphore]:
        if limit <= 0:
            self._semaphore = None
            self._limit = 0
            return None
        if self._semaphore is None or limit != self._limit:
            # Rebuilding drops the old waiters' ordering, which is acceptable:
            # the limit only changes when an operator edits it.
            self._semaphore = asyncio.Semaphore(limit)
            self._limit = limit
        return self._semaphore

    @contextlib.asynccontextmanager
    async def hold(self, limit: int, timeout: float, label: str) -> AsyncIterator[None]:
        semaphore = self._resolve(limit)
        if semaphore is None:
            yield
            return

        self._waiting += 1
        try:
            if timeout > 0:
                try:
                    await asyncio.wait_for(semaphore.acquire(), timeout=timeout)
                except asyncio.TimeoutError:
                    raise QueueTimeout(
                        f"waited {timeout:.0f}s for a {label} slot with {self._limit} in flight"
                    ) from None
            else:
                await semaphore.acquire()
        finally:
            self._waiting -= 1

        self._held += 1
        try:
            yield
        finally:
            self._held -= 1
            semaphore.release()

    @property
    def stats(self) -> dict[str, int]:
        return {"limit": self._limit, "held": self._held, "waiting": self._waiting}


_global_gate = _Gate()
_account_gates: dict[str, _Gate] = {}


def reset() -> None:
    """Drop every gate. Used by tests and after a limit change."""
    global _global_gate
    _global_gate = _Gate()
    _account_gates.clear()


@contextlib.asynccontextmanager
async def slot(account_id: Optional[str] = None) -> AsyncIterator[None]:
    """Hold a slot for the duration of one generation request."""
    timeout = float(QUEUE_TIMEOUT_SECONDS.value())
    global_limit = int(MAX_CONCURRENCY.value())
    account_limit = int(MAX_ACCOUNT_CONCURRENCY.value())

    if global_limit <= 0 and account_limit <= 0:
        yield
        return

    async with _global_gate.hold(global_limit, timeout, "global"):
        if account_limit <= 0 or not account_id:
            yield
            return
        gate = _account_gates.get(account_id)
        if gate is None:
            gate = _Gate()
            _account_gates[account_id] = gate
        async with gate.hold(account_limit, timeout, f"account {account_id}"):
            yield


def status() -> dict[str, object]:
    return {
        "global": _global_gate.stats,
        "accounts": {account_id: gate.stats for account_id, gate in _account_gates.items() if gate.stats["held"] or gate.stats["waiting"]},
        "queueTimeoutSeconds": QUEUE_TIMEOUT_SECONDS.value(),
    }
