# -*- coding: utf-8 -*-
"""Fetches the model catalogue Kiro publishes for an account.

The runtime host has no ``/ListAvailableModels``, which is why the gateway fell
back to a hardcoded list. The operation does exist though: it is served by the
management host as an AWS JSON call, the same way the usage query is, and it is
what the official Kiro CLI calls. Verified against runtime and management on
2026-08-31: the runtime GET answers 404 UnknownOperationException, the management
POST answers 200 with 19 models.

Using it means the model list, the token limits and the presentable names come
from the account instead of from a constant that has to be edited by hand every
time Kiro ships a model.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from loguru import logger

from kiro.utils import get_kiro_headers

TARGET = "AmazonCodeWhispererService.ListAvailableModels"
TIMEOUT = 20.0


def _region(auth_manager: Any) -> str:
    """Region for the management host: the ARN wins, then the resolved API host."""
    profile_arn = getattr(auth_manager, "profile_arn", None) or ""
    parts = profile_arn.split(":")
    if len(parts) >= 4 and parts[2] == "codewhisperer":
        return parts[3]
    return getattr(auth_manager, "api_region", None) or "us-east-1"


async def fetch_available_models(auth_manager: Any) -> Optional[list[dict[str, Any]]]:
    """Return the account's model list, or None when it cannot be retrieved.

    None means "keep whatever you had": a failure here must not empty a working
    catalogue, and the caller falls back to the static list on first load.
    """
    try:
        token = await auth_manager.get_access_token()
    except Exception as exc:
        logger.debug(f"[Models] No token to list models: {type(exc).__name__}: {exc}")
        return None

    profile_arn = getattr(auth_manager, "request_profile_arn", None) or getattr(auth_manager, "profile_arn", None)
    params: dict[str, str] = {"origin": "AI_EDITOR"}
    body: dict[str, Any] = {"origin": "AI_EDITOR"}
    if profile_arn:
        # The upstream rejects the call with "Invalid profileArn" when the body
        # omits it, so both carry the value, as the CLI does.
        params["profileArn"] = profile_arn
        body["profileArn"] = profile_arn

    headers = get_kiro_headers(auth_manager, token)
    headers["x-amz-target"] = TARGET
    headers["Accept"] = "application/json"
    url = f"https://management.{_region(auth_manager)}.kiro.dev/"

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(url, params=params, json=body, headers=headers)
        if response.status_code != 200:
            logger.debug(f"[Models] Management host answered {response.status_code} for the model list")
            return None
        models = response.json().get("models")
    except Exception as exc:
        logger.debug(f"[Models] Could not list models: {type(exc).__name__}: {exc}")
        return None

    if not isinstance(models, list) or not models:
        return None
    return [item for item in models if isinstance(item, dict) and item.get("modelId")]
