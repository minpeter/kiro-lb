# -*- coding: utf-8 -*-
"""Ordered proxy list with failover.

A single proxy is a single point of failure: when it drops, every request fails.
This keeps an ordered list and moves to the next entry when one stops working,
with a cooldown so a dead proxy is not retried on every request.

Each proxy gets its own httpx client, because the proxy is fixed at client
construction. Clients are cached and reused, so connection pooling still applies.

Accepted forms, one per entry:
    http://user:pass@host:8080
    socks5://host:1080          DNS resolved locally
    socks5h://host:1080         DNS resolved by the proxy
    socks5|user:pass@host:1080  the scheme|rest form
    host:8080                   assumes http
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from loguru import logger

from kiro import store

SETTING_KEY = "proxy_chain"
COOLDOWN_SECONDS = 60.0
SUPPORTED_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4", "socks4a")

_CREDENTIALS = re.compile(r"://([^:/@]+):([^@]*)@")


@dataclass(frozen=True)
class ProxyEntry:
    url: str

    @property
    def masked(self) -> str:
        """The url with any password replaced, for logs and the dashboard."""
        return _CREDENTIALS.sub(lambda m: f"://{m.group(1)}:***@", self.url)


class InvalidProxy(ValueError):
    """Raised when an entry cannot be turned into a usable proxy url."""


def normalize(raw: Any) -> str:
    """Turn one accepted form into a url httpx understands."""
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidProxy("proxy must be a non-empty string")
    candidate = raw.strip()

    if "|" in candidate and "://" not in candidate:
        scheme, _, rest = candidate.partition("|")
        scheme = scheme.strip().lower()
        if scheme not in SUPPORTED_SCHEMES:
            raise InvalidProxy(f"unsupported scheme {scheme!r}; use one of {', '.join(SUPPORTED_SCHEMES)}")
        candidate = f"{scheme}://{rest.strip()}"
    elif "://" not in candidate:
        candidate = f"http://{candidate}"

    scheme = candidate.split("://", 1)[0].lower()
    if scheme not in SUPPORTED_SCHEMES:
        raise InvalidProxy(f"unsupported scheme {scheme!r}; use one of {', '.join(SUPPORTED_SCHEMES)}")

    try:
        parsed = httpx.URL(candidate)
    except Exception as exc:
        raise InvalidProxy(f"could not parse {raw!r}: {exc}") from None
    if not parsed.host:
        raise InvalidProxy(f"missing host in {raw!r}")
    return candidate


def validate(entries: Any) -> list[str]:
    """Normalize a list of entries, rejecting the whole list on a bad one."""
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise InvalidProxy("expected a list of proxies")
    seen: list[str] = []
    for entry in entries:
        url = normalize(entry)
        if url not in seen:
            seen.append(url)
    return seen


_lock = threading.Lock()
_chain: Optional[list[ProxyEntry]] = None
_cooldowns: dict[str, float] = {}
_clients: dict[str, httpx.AsyncClient] = {}
_client_settings: dict[str, Any] = {}


def configure_clients(limits: httpx.Limits, timeout: httpx.Timeout) -> None:
    """Record how per-proxy clients should be built, matching the shared one."""
    _client_settings["limits"] = limits
    _client_settings["timeout"] = timeout


def chain() -> list[ProxyEntry]:
    global _chain
    if _chain is None:
        with _lock:
            if _chain is None:
                _chain = []
    return _chain


def reset_cache() -> None:
    global _chain
    with _lock:
        _chain = None
        _cooldowns.clear()


def load_from_store() -> list[ProxyEntry]:
    global _chain
    stored = store.load_setting(SETTING_KEY)
    entries: list[ProxyEntry] = []
    if stored:
        try:
            entries = [ProxyEntry(url) for url in validate(stored)]
        except InvalidProxy as exc:
            logger.warning(f"[Proxy] Ignoring persisted chain: {exc}")
            entries = []
    with _lock:
        _chain = entries
        _cooldowns.clear()
    if entries:
        logger.info(f"[Proxy] Chain: {', '.join(entry.masked for entry in entries)}")
    return entries


def set_chain(entries: Any) -> list[ProxyEntry]:
    global _chain
    urls = validate(entries)
    store.save_setting(SETTING_KEY, urls)
    resolved = [ProxyEntry(url) for url in urls]
    with _lock:
        _chain = resolved
        _cooldowns.clear()
    logger.info(
        f"[Proxy] Chain set to {', '.join(entry.masked for entry in resolved)}" if resolved else "[Proxy] Chain cleared"
    )
    return resolved


def is_cooling(url: str, now: Optional[float] = None) -> bool:
    now = time.monotonic() if now is None else now
    until = _cooldowns.get(url)
    if until is None:
        return False
    if until <= now:
        del _cooldowns[url]
        return False
    return True


def record_failure(url: str) -> None:
    _cooldowns[url] = time.monotonic() + COOLDOWN_SECONDS
    logger.warning(f"[Proxy] {ProxyEntry(url).masked} failed; cooling for {COOLDOWN_SECONDS:.0f}s")


def record_success(url: str) -> None:
    _cooldowns.pop(url, None)


def attempt_order() -> list[ProxyEntry]:
    """Ready proxies first, cooling ones last. Nothing is dropped."""
    entries = chain()
    if not entries:
        return []
    ready = [entry for entry in entries if not is_cooling(entry.url)]
    cooling = [entry for entry in entries if is_cooling(entry.url)]
    return ready + cooling


async def client_for(url: str) -> httpx.AsyncClient:
    """A cached client bound to one proxy."""
    existing = _clients.get(url)
    if existing is not None and not existing.is_closed:
        return existing
    created = httpx.AsyncClient(
        proxy=url,
        limits=_client_settings.get("limits") or httpx.Limits(max_connections=100, max_keepalive_connections=20),
        timeout=_client_settings.get("timeout") or httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0),
        follow_redirects=True,
    )
    _clients[url] = created
    return created


async def close_clients() -> None:
    for client in list(_clients.values()):
        try:
            await client.aclose()
        except Exception:
            pass
    _clients.clear()


def status() -> list[dict[str, Any]]:
    return [{"url": entry.masked, "cooling": is_cooling(entry.url)} for entry in chain()]
