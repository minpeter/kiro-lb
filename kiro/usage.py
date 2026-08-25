# -*- coding: utf-8 -*-
"""Kiro account subscription and usage polling.

This module queries Kiro's authenticated usage endpoint and returns a compact,
non-secret summary suitable for the private operations dashboard. It never
persists access tokens, profile ARNs, or the raw upstream response.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from kiro.account_manager import Account
from kiro.auth import AuthType
from kiro.config import KIRO_BUILDER_ID_PROFILE_ARN
from kiro.utils import get_kiro_headers


def _usage_region(account: Account) -> str:
    """Resolve the Kiro data-plane region for usage queries.

    The profile ARN is authoritative. Otherwise derive the region from the
    resolved API host, which already accounts for per-account overrides.
    """
    assert account.auth_manager is not None
    profile_arn = account.auth_manager.profile_arn or ""
    parts = profile_arn.split(":")
    if len(parts) >= 4 and parts[2] == "codewhisperer":
        return parts[3]

    host = account.auth_manager.api_host or ""
    match = re.search(r"://(?:runtime|q)\.([a-z0-9-]+)\.", host)
    if match:
        return match.group(1)
    return account.auth_manager.region or "us-east-1"


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


async def fetch_account_usage(account: Account) -> dict[str, Any]:
    """Fetch and normalize the live Kiro subscription usage for one account."""
    if account.auth_manager is None:
        raise RuntimeError("Account is not initialized")

    auth = account.auth_manager
    token = await auth.get_access_token()
    params: dict[str, str] = {
        "origin": "AI_EDITOR",
        "isEmailRequired": "true",
    }
    body: dict[str, str | bool] = {
        "origin": "AI_EDITOR",
        "isEmailRequired": True,
    }
    profile_arn = auth.profile_arn
    if profile_arn is None and auth.auth_type == AuthType.AWS_SSO_OIDC:
        profile_arn = KIRO_BUILDER_ID_PROFILE_ARN
    if profile_arn:
        params["profileArn"] = profile_arn
        body["profileArn"] = profile_arn
    url = f"https://management.{_usage_region(account)}.kiro.dev/"
    headers = get_kiro_headers(auth, token)
    headers["x-amz-target"] = "AmazonCodeWhispererService.GetUsageLimits"
    headers["Accept"] = "application/json"

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(url, params=params, json=body, headers=headers)
    response.raise_for_status()
    payload = response.json()

    breakdowns = payload.get("usageBreakdownList") or []
    breakdown = breakdowns[0] if breakdowns else {}
    subscription = payload.get("subscriptionInfo") or {}
    overage = payload.get("overageConfiguration") or {}
    # The request asks for the identity block (isEmailRequired), which is the
    # only way to tell two accounts apart on the dashboard: account IDs are
    # hashed credential paths. The user ID stays out; it identifies nothing an
    # operator can act on.
    user_info = payload.get("userInfo") or {}
    # Credit usage is fractional: the integer fields round 695.17 down to 695,
    # so prefer the precise values and fall back only when they are absent.
    current = _number(breakdown.get("currentUsageWithPrecision"))
    if current is None:
        current = _number(breakdown.get("currentUsage"))
    limit = _number(breakdown.get("usageLimitWithPrecision"))
    if limit is None:
        limit = _number(breakdown.get("usageLimit"))
    overage_used = _number(breakdown.get("currentOveragesWithPrecision"))
    if overage_used is None:
        overage_used = _number(breakdown.get("currentOverages"))
    return {
        "email": user_info.get("email") or None,
        "subscriptionTitle": subscription.get("subscriptionTitle") or subscription.get("type") or "Unknown",
        "subscriptionType": subscription.get("type") or "Unknown",
        "resourceType": breakdown.get("resourceType") or "AGENTIC_REQUEST",
        "currentUsage": current,
        "usageLimit": limit,
        "usagePercent": (current / limit * 100) if current is not None and limit and limit > 0 else None,
        "unit": breakdown.get("unit") or "",
        "overageStatus": overage.get("overageStatus") or "UNKNOWN",
        "overageUsed": overage_used,
        "overageRate": _number(breakdown.get("overageRate")),
        "nextDateReset": payload.get("nextDateReset"),
        "daysUntilReset": payload.get("daysUntilReset"),
    }
