# -*- coding: utf-8 -*-
"""Scalar settings the operator can change from the dashboard.

Three modules already carry their own copy of the same pattern - environment
default, in-memory cache, load, validate, persist. Anything simpler than the
endpoint configuration belongs here instead of growing a fourth and fifth copy.

Reads are on the request path, so ``value()`` never touches SQLite.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Generic, Optional, TypeVar

from loguru import logger

from kiro import store

T = TypeVar("T")


class InvalidSetting(ValueError):
    """Raised when a requested value is outside what the setting accepts."""


class Tunable(Generic[T]):
    """One persisted scalar, cached in memory and validated on write."""

    def __init__(self, key: str, default: T, coerce: Callable[[Any], T]):
        self._key = key
        self._default = default
        self._coerce = coerce
        self._lock = threading.Lock()
        self._cached: Optional[T] = None

    @property
    def key(self) -> str:
        return self._key

    def value(self) -> T:
        cached = self._cached
        if cached is None:
            with self._lock:
                if self._cached is None:
                    self._cached = self._default
                cached = self._cached
        return cached

    def reset_cache(self) -> None:
        with self._lock:
            self._cached = None

    def validate(self, raw: Any) -> T:
        return self._coerce(raw)

    def load(self) -> T:
        stored = store.load_setting(self._key)
        resolved = self._default
        if stored is not None:
            try:
                resolved = self._coerce(stored)
            except InvalidSetting as exc:
                logger.warning(f"[Tunables] Ignoring persisted {self._key}: {exc}")
        with self._lock:
            self._cached = resolved
        return resolved

    def set(self, raw: Any) -> T:
        resolved = self._coerce(raw)
        store.save_setting(self._key, resolved)
        with self._lock:
            self._cached = resolved
        logger.info(f"[Tunables] {self._key} set to {resolved!r}")
        return resolved


def bounded_int(low: int, high: int) -> Callable[[Any], int]:
    def coerce(raw: Any) -> int:
        if isinstance(raw, bool):
            raise InvalidSetting("expected a number, got a boolean")
        try:
            number = int(raw)
        except (TypeError, ValueError):
            raise InvalidSetting(f"expected a whole number, got {raw!r}") from None
        if not low <= number <= high:
            raise InvalidSetting(f"must be between {low} and {high}")
        return number

    return coerce


def boolean() -> Callable[[Any], bool]:
    def coerce(raw: Any) -> bool:
        if isinstance(raw, bool):
            return raw
        raise InvalidSetting("expected true or false")

    return coerce


def one_of(allowed: tuple[str, ...]) -> Callable[[Any], str]:
    def coerce(raw: Any) -> str:
        if not isinstance(raw, str):
            raise InvalidSetting("expected a string")
        candidate = raw.strip()
        if candidate not in allowed:
            raise InvalidSetting(f"must be one of {', '.join(repr(item) for item in allowed)}")
        return candidate

    return coerce
