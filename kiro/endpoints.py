# -*- coding: utf-8 -*-
"""Generation endpoint rotation.

The primary endpoint is ``runtime.{region}.kiro.dev``, the one current Kiro CLI
clients use. With a single URL there is nowhere to go when it degrades: the
request fails outright. This module declares the known alternates and keeps the
minimum state needed to rotate between them.

State is per-process and in memory. A restart falls back to the declared order.

Only ``runtime`` is verified for every credential type. The alternates exist for
failover and may reject some accounts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from loguru import logger

GENERATE_TARGET = "AmazonCodeWhispererStreamingService.GenerateAssistantResponse"


@dataclass(frozen=True)
class KiroEndpoint:
    """A generation destination and the dialect it expects."""

    key: str
    name: str
    url_template: str
    amz_target: Optional[str] = GENERATE_TARGET
    content_type: str = "application/x-amz-json-1.0"
    regional: bool = True

    def url(self, region: str) -> str:
        return self.url_template.format(region=region) if self.regional else self.url_template

    def header_overrides(self) -> Dict[str, str]:
        overrides: Dict[str, str] = {"Content-Type": self.content_type}
        if self.amz_target:
            overrides["x-amz-target"] = self.amz_target
        return overrides


# Declared order is the attempt order when no affinity is recorded.
KIRO_ENDPOINTS: Tuple[KiroEndpoint, ...] = (
    KiroEndpoint(
        key="runtime",
        name="Kiro Runtime",
        url_template="https://runtime.{region}.kiro.dev/",
    ),
    KiroEndpoint(
        key="codewhisperer",
        name="CodeWhisperer",
        url_template="https://codewhisperer.{region}.amazonaws.com/generateAssistantResponse",
    ),
    KiroEndpoint(
        key="amazonq",
        name="AmazonQ",
        url_template="https://q.{region}.amazonaws.com/generateAssistantResponse",
        amz_target="AmazonQDeveloperStreamingService.SendMessage",
    ),
)

ENDPOINTS_BY_KEY: Dict[str, KiroEndpoint] = {endpoint.key: endpoint for endpoint in KIRO_ENDPOINTS}


@dataclass
class _RotationState:
    affinity: Dict[Tuple[str, str], str] = field(default_factory=dict)
    cooldown_until: Dict[str, float] = field(default_factory=dict)


_state = _RotationState()


def reset_state() -> None:
    """Clear affinity and cooldown. Used by tests."""
    _state.affinity.clear()
    _state.cooldown_until.clear()


def selected_endpoints(order: Optional[List[str]] = None) -> List[KiroEndpoint]:
    """Resolve the configured order, ignoring unknown keys."""
    if not order:
        return list(KIRO_ENDPOINTS)
    resolved: List[KiroEndpoint] = []
    for key in order:
        key = key.strip()
        if not key:
            continue
        endpoint = ENDPOINTS_BY_KEY.get(key)
        if endpoint is None:
            logger.warning(f"[Endpoints] Ignoring unknown key: {key!r}")
            continue
        if endpoint not in resolved:
            resolved.append(endpoint)
    return resolved or list(KIRO_ENDPOINTS)


def is_cooling(endpoint_key: str, now: Optional[float] = None) -> bool:
    now = time.monotonic() if now is None else now
    until = _state.cooldown_until.get(endpoint_key)
    if until is None:
        return False
    if until <= now:
        del _state.cooldown_until[endpoint_key]
        return False
    return True


def record_failure(endpoint_key: str, cooldown_seconds: float) -> None:
    if cooldown_seconds <= 0:
        return
    _state.cooldown_until[endpoint_key] = time.monotonic() + cooldown_seconds
    logger.debug(f"[Endpoints] {endpoint_key} cooling for {cooldown_seconds:.0f}s")


def record_success(account_key: str, model: str, endpoint_key: str) -> None:
    _state.affinity[(account_key, model)] = endpoint_key
    _state.cooldown_until.pop(endpoint_key, None)


def attempt_order(
    account_key: str,
    model: str,
    order: Optional[List[str]] = None,
    cooldown_seconds: float = 0.0,
) -> List[KiroEndpoint]:
    """Return the endpoints in attempt order.

    Affinity first, then the declared order, with cooling endpoints moved to the
    back. Nothing is ever removed: a cooldown must not turn a request into a
    hard failure.
    """
    endpoints = selected_endpoints(order)
    preferred_key = _state.affinity.get((account_key, model))

    ranked: List[KiroEndpoint] = []
    if preferred_key and preferred_key in ENDPOINTS_BY_KEY:
        preferred = ENDPOINTS_BY_KEY[preferred_key]
        if preferred in endpoints and not is_cooling(preferred_key):
            ranked.append(preferred)

    ready = [e for e in endpoints if e not in ranked and not is_cooling(e.key)]
    cooling = [e for e in endpoints if e not in ranked and is_cooling(e.key)]
    return ranked + ready + cooling
