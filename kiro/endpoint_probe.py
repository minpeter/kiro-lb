# -*- coding: utf-8 -*-
"""Connectivity and latency probes for the generation endpoints.

These run on operator demand from the dashboard and spend real quota, so the
payload is the smallest one the API accepts and a lock keeps concurrent runs out.

The probes deliberately bypass ``request_with_retry``: they must not write to the
rotation affinity or cooldown state, otherwise measuring would change routing.
"""

from __future__ import annotations

import asyncio
import statistics
import time
import uuid
from typing import Any, Optional

import httpx
from loguru import logger

from kiro import proxy_chain
from kiro.endpoints import KIRO_ENDPOINTS, KiroEndpoint
from kiro.utils import get_kiro_headers

PROBE_PROMPT = "Reply with the single word: ok"
PROBE_TIMEOUT = 45.0
PING_REPS_MIN = 1
PING_REPS_MAX = 10
PING_REPS_DEFAULT = 1

_probe_lock = asyncio.Lock()


def _probe_client() -> httpx.AsyncClient:
    """Measure the path production requests actually take: when a proxy chain
    is configured, a direct probe would report connectivity the data plane
    does not have."""
    order = proxy_chain.attempt_order()
    proxy = order[0].url if order else None
    return httpx.AsyncClient(timeout=httpx.Timeout(PROBE_TIMEOUT, connect=10.0), proxy=proxy)


class ProbeBusy(RuntimeError):
    """Raised when a probe is already running."""


class ProbeUnavailable(RuntimeError):
    """Raised when no usable account is available to probe with."""


def _payload(model: str, profile_arn: Optional[str]) -> dict[str, Any]:
    """The smallest generation request the API accepts."""
    body: dict[str, Any] = {
        "conversationState": {
            "chatTriggerType": "MANUAL",
            "conversationId": str(uuid.uuid4()),
            "currentMessage": {
                "userInputMessage": {
                    "content": PROBE_PROMPT,
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


async def _resolve_account(account_manager: Any, model: str):
    account = await account_manager.get_next_account(model)
    if account is None:
        try:
            account = account_manager.get_first_account()
        except Exception as exc:
            raise ProbeUnavailable("no account is available to probe with") from exc
    if account is None:
        raise ProbeUnavailable("no account is available to probe with")
    return account


async def _single_attempt(
    client: httpx.AsyncClient,
    endpoint: KiroEndpoint,
    account: Any,
    model: str,
) -> dict[str, Any]:
    """Send one probe and report status plus time to first byte."""
    auth = account.auth_manager
    region = auth.api_region
    url = endpoint.url(region)

    token = await auth.get_access_token()
    headers = get_kiro_headers(auth, token)
    headers.update(endpoint.header_overrides())
    profile_arn = getattr(auth, "request_profile_arn", None) or getattr(auth, "profile_arn", None)
    if profile_arn:
        headers["x-amzn-kiro-profile-arn"] = profile_arn

    started = time.perf_counter()
    try:
        async with client.stream("POST", url, json=_payload(model, profile_arn), headers=headers) as response:
            ttfb_ms: Optional[float] = None
            received = 0
            async for chunk in response.aiter_raw():
                if ttfb_ms is None:
                    ttfb_ms = (time.perf_counter() - started) * 1000
                received += len(chunk)
                if received > 2048:
                    break
            if ttfb_ms is None:
                ttfb_ms = (time.perf_counter() - started) * 1000
            return {
                "ok": response.status_code == 200,
                "statusCode": response.status_code,
                "ttfbMs": round(ttfb_ms),
                "error": None if response.status_code == 200 else f"HTTP {response.status_code}",
            }
    except Exception as exc:
        return {
            "ok": False,
            "statusCode": None,
            "ttfbMs": None,
            "error": f"{type(exc).__name__}: {exc}"[:200],
        }


def _selected(only: Optional[str]) -> tuple[KiroEndpoint, ...]:
    """One endpoint when named, otherwise all of them."""
    if not only:
        return KIRO_ENDPOINTS
    chosen = tuple(endpoint for endpoint in KIRO_ENDPOINTS if endpoint.key == only)
    if not chosen:
        raise ProbeUnavailable(f"unknown endpoint {only!r}")
    return chosen


async def test_endpoints(
    account_manager: Any, model: Optional[str] = None, only: Optional[str] = None
) -> dict[str, Any]:
    """Send one probe per endpoint and report whether each accepts the account."""
    if _probe_lock.locked():
        raise ProbeBusy("a probe is already running")

    targets = _selected(only)
    async with _probe_lock:
        resolved_model = model or "claude-sonnet-4.5"
        account = await _resolve_account(account_manager, resolved_model)
        results: list[dict[str, Any]] = []

        async with _probe_client() as client:
            for endpoint in targets:
                outcome = await _single_attempt(client, endpoint, account, resolved_model)
                results.append({"key": endpoint.key, "name": endpoint.name, **outcome})
                logger.info(f"[Probe] {endpoint.key} test: status={outcome['statusCode']} ttfb={outcome['ttfbMs']}")

        return {
            "model": resolved_model,
            "requestsSpent": len(results),
            "results": results,
        }


async def ping_endpoints(
    account_manager: Any,
    reps: int = PING_REPS_DEFAULT,
    model: Optional[str] = None,
    only: Optional[str] = None,
) -> dict[str, Any]:
    """Measure time to first byte per endpoint over interleaved repetitions.

    Repetitions are interleaved rather than grouped so a drift in upstream
    latency hits every endpoint alike. The verdict compares the spread between
    endpoints against the widest spread inside a single endpoint: a difference
    smaller than one endpoint's own variance is not evidence of anything.
    """
    if _probe_lock.locked():
        raise ProbeBusy("a probe is already running")

    reps = max(PING_REPS_MIN, min(PING_REPS_MAX, int(reps)))
    targets = _selected(only)

    async with _probe_lock:
        resolved_model = model or "claude-sonnet-4.5"
        account = await _resolve_account(account_manager, resolved_model)

        samples: dict[str, list[float]] = {e.key: [] for e in targets}
        failures: dict[str, list[str]] = {e.key: [] for e in targets}

        async with _probe_client() as client:
            for _ in range(reps):
                for endpoint in targets:
                    outcome = await _single_attempt(client, endpoint, account, resolved_model)
                    if outcome["ok"] and outcome["ttfbMs"] is not None:
                        samples[endpoint.key].append(float(outcome["ttfbMs"]))
                    else:
                        failures[endpoint.key].append(outcome["error"] or "unknown")

        results = []
        for endpoint in targets:
            values = samples[endpoint.key]
            results.append(
                {
                    "key": endpoint.key,
                    "name": endpoint.name,
                    "samples": len(values),
                    "medianMs": round(statistics.median(values)) if values else None,
                    "minMs": round(min(values)) if values else None,
                    "maxMs": round(max(values)) if values else None,
                    "failures": failures[endpoint.key],
                }
            )

        return {
            "model": resolved_model,
            "reps": reps,
            "requestsSpent": reps * len(targets),
            "results": results,
            **_verdict(samples),
        }


def _verdict(samples: dict[str, list[float]]) -> dict[str, Any]:
    """Say whether the ranking is meaningful, and refuse to overclaim."""
    usable = {key: values for key, values in samples.items() if values}
    if not usable:
        return {"fastest": None, "conclusive": False, "verdict": "No endpoint answered."}

    medians = {key: statistics.median(values) for key, values in usable.items()}
    fastest = min(medians, key=lambda key: medians[key])
    if len(usable) == 1:
        return {
            "fastest": fastest,
            "conclusive": False,
            "verdict": f"Only {fastest} answered; nothing to compare against.",
        }

    between = max(medians.values()) - min(medians.values())
    within = max(max(values) - min(values) for values in usable.values())
    conclusive = between > within
    if conclusive:
        verdict = (
            f"{fastest} is fastest by {round(between)}ms, which exceeds the widest "
            f"single-endpoint spread of {round(within)}ms."
        )
    else:
        verdict = (
            f"Indistinguishable: the {round(between)}ms gap between endpoints is smaller "
            f"than the {round(within)}ms spread within one endpoint. Raise repetitions "
            f"for a firmer answer."
        )
    return {
        "fastest": fastest,
        "conclusive": conclusive,
        "betweenSpreadMs": round(between),
        "withinSpreadMs": round(within),
        "verdict": verdict,
    }
