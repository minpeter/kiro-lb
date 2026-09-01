# -*- coding: utf-8 -*-
"""Runtime-mutable endpoint settings.

The environment supplies the defaults. The dashboard may override them, and the
override is persisted so a restart keeps the operator's choice.

Reads happen on every generation request, so ``current()`` returns a cached
immutable snapshot and never touches SQLite. Only ``update()`` and
``load_from_store()`` hit the database.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from loguru import logger

from kiro import store
from kiro.config import (
    KIRO_ENDPOINT_COOLDOWN_SECONDS,
    KIRO_ENDPOINT_ORDER,
    KIRO_ENDPOINT_ROTATION,
)
from kiro.endpoints import ENDPOINTS_BY_KEY, KIRO_ENDPOINTS

SETTING_KEY = "endpoints"

COOLDOWN_MIN = 0.0
COOLDOWN_MAX = 3600.0


class InvalidEndpointSettings(ValueError):
    """Raised when a requested override would be unusable."""


@dataclass(frozen=True)
class EndpointSettings:
    """An immutable snapshot of the endpoint configuration."""

    rotation: bool
    order: tuple[str, ...]
    cooldown_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "rotation": self.rotation,
            "order": list(self.order),
            "cooldownSeconds": self.cooldown_seconds,
        }


def _env_defaults() -> EndpointSettings:
    return EndpointSettings(
        rotation=KIRO_ENDPOINT_ROTATION,
        order=tuple(_known_keys(KIRO_ENDPOINT_ORDER)) or tuple(e.key for e in KIRO_ENDPOINTS),
        cooldown_seconds=float(KIRO_ENDPOINT_COOLDOWN_SECONDS),
    )


def _known_keys(keys: Iterable[Any]) -> list[str]:
    """Keep recognised keys, in order, without duplicates."""
    seen: list[str] = []
    for key in keys:
        if not isinstance(key, str):
            continue
        key = key.strip()
        if key and key in ENDPOINTS_BY_KEY and key not in seen:
            seen.append(key)
    return seen


_lock = threading.Lock()
_cached: Optional[EndpointSettings] = None


def current() -> EndpointSettings:
    """Return the active settings. Safe to call per request: no I/O."""
    global _cached
    if _cached is None:
        with _lock:
            if _cached is None:
                _cached = _env_defaults()
    return _cached


def reset_cache() -> None:
    """Drop the cached snapshot so the next read rebuilds it. Used by tests."""
    global _cached
    with _lock:
        _cached = None


def validate(
    rotation: Any,
    order: Any,
    cooldown_seconds: Any,
) -> EndpointSettings:
    """Coerce and check an override, raising when it would be unusable.

    An empty order is rejected rather than silently replaced: accepting it would
    leave the gateway with nowhere to send generation requests, and the operator
    would see a saved setting that does not match what runs.
    """
    if not isinstance(rotation, bool):
        raise InvalidEndpointSettings("rotation must be a boolean")

    if not isinstance(order, Sequence) or isinstance(order, (str, bytes)):
        raise InvalidEndpointSettings("order must be a list of endpoint keys")

    unknown = [key for key in order if not isinstance(key, str) or key.strip() not in ENDPOINTS_BY_KEY]
    if unknown:
        known = ", ".join(sorted(ENDPOINTS_BY_KEY))
        raise InvalidEndpointSettings(f"unknown endpoint keys: {unknown!r}; known keys are {known}")

    resolved = _known_keys(order)
    if not resolved:
        raise InvalidEndpointSettings("at least one endpoint must stay enabled")

    try:
        cooldown = float(cooldown_seconds)
    except (TypeError, ValueError):
        raise InvalidEndpointSettings("cooldownSeconds must be a number") from None
    if not COOLDOWN_MIN <= cooldown <= COOLDOWN_MAX:
        raise InvalidEndpointSettings(f"cooldownSeconds must be between {COOLDOWN_MIN} and {COOLDOWN_MAX}")

    return EndpointSettings(rotation=rotation, order=tuple(resolved), cooldown_seconds=cooldown)


def load_from_store() -> EndpointSettings:
    """Adopt the persisted override, falling back to the environment.

    Called once at startup. A malformed or stale row is discarded with a warning
    instead of failing the boot.
    """
    global _cached
    payload = store.load_setting(SETTING_KEY)
    settings = _env_defaults()
    if isinstance(payload, dict):
        try:
            settings = validate(
                payload.get("rotation", settings.rotation),
                payload.get("order", list(settings.order)),
                payload.get("cooldownSeconds", settings.cooldown_seconds),
            )
        except InvalidEndpointSettings as exc:
            logger.warning(f"[Endpoints] Ignoring persisted settings: {exc}")
            settings = _env_defaults()
    with _lock:
        _cached = settings
    return settings


def update(rotation: Any, order: Any, cooldown_seconds: Any) -> EndpointSettings:
    """Validate, persist, then publish. Nothing is cached if the write fails."""
    global _cached
    settings = validate(rotation, order, cooldown_seconds)
    store.save_setting(SETTING_KEY, settings.as_dict())
    with _lock:
        _cached = settings
    logger.info(
        f"[Endpoints] Settings updated: rotation={settings.rotation} "
        f"order={list(settings.order)} cooldown={settings.cooldown_seconds:.0f}s"
    )
    return settings
