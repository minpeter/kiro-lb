# -*- coding: utf-8 -*-
"""Runtime-mutable agent task mode.

``conversationState.agentTaskType`` is what the official Kiro CLI sends to
declare the kind of work in flight. It sends ``vibe`` for free-form chat, and
``spec`` and ``task`` for its structured modes. Neither gateway sent the field
at all before, so the upstream saw no mode.

The effect of each mode upstream is not documented, so the value is
configurable and an empty value omits the field entirely, restoring the
previous payload shape.

Reads happen per request, so ``current()`` returns a cached value and never
touches SQLite.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from loguru import logger

from kiro import store
from kiro.config import KIRO_AGENT_TASK_TYPE

SETTING_KEY = "agent_task_type"

OMITTED = ""
ALLOWED_MODES: tuple[str, ...] = (OMITTED, "vibe", "spec", "task")


class InvalidAgentMode(ValueError):
    """Raised when a requested mode is not one the CLI is known to send."""


def _env_default() -> str:
    candidate = (KIRO_AGENT_TASK_TYPE or "").strip()
    if candidate not in ALLOWED_MODES:
        logger.warning(f"[AgentMode] Ignoring unknown KIRO_AGENT_TASK_TYPE={candidate!r}; omitting the field")
        return OMITTED
    return candidate


_lock = threading.Lock()
_cached: Optional[str] = None


def current() -> str:
    """Return the active mode, or an empty string to omit the field."""
    global _cached
    if _cached is None:
        with _lock:
            if _cached is None:
                _cached = _env_default()
    return _cached


def reset_cache() -> None:
    """Drop the cached value so the next read rebuilds it. Used by tests."""
    global _cached
    with _lock:
        _cached = None


def validate(value: Any) -> str:
    """Coerce and check a mode, raising when it is not a known one."""
    if value is None:
        return OMITTED
    if not isinstance(value, str):
        raise InvalidAgentMode("mode must be a string")
    candidate = value.strip()
    if candidate not in ALLOWED_MODES:
        known = ", ".join(repr(mode) for mode in ALLOWED_MODES)
        raise InvalidAgentMode(f"unknown mode {candidate!r}; allowed values are {known}")
    return candidate


def load_from_store() -> str:
    """Adopt the persisted mode, falling back to the environment default."""
    global _cached
    payload = store.load_setting(SETTING_KEY)
    resolved = _env_default()
    if payload is not None:
        try:
            resolved = validate(payload)
        except InvalidAgentMode as exc:
            logger.warning(f"[AgentMode] Ignoring persisted mode: {exc}")
    with _lock:
        _cached = resolved
    return resolved


def set_mode(value: Any) -> str:
    """Validate, persist, then publish the mode."""
    global _cached
    mode = validate(value)
    store.save_setting(SETTING_KEY, mode)
    with _lock:
        _cached = mode
    logger.info(f"[AgentMode] agentTaskType set to {mode!r}" if mode else "[AgentMode] agentTaskType omitted")
    return mode
