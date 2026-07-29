# -*- coding: utf-8 -*-

# kiro-lb
# https://github.com/minpeter/kiro-lb
# Copyright (C) 2026 minpeter
#
# Derived from Kiro Gateway (https://github.com/jwadow/kiro-gateway),
# Copyright (C) 2025 Jwadow.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Kiro account subscription and usage polling.

This module queries Kiro's authenticated usage endpoint and returns a compact,
non-secret summary suitable for the private operations dashboard. It never
persists access tokens, profile ARNs, or the raw upstream response.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

import httpx

from kiro.account_manager import Account
from kiro.utils import get_kiro_headers


def _usage_region(account: Account) -> str:
    """Resolve the Kiro data-plane region for usage queries.

    The profile ARN is authoritative. Otherwise derive the region from the
    resolved API host, which already accounts for per-account overrides.
    """
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
    params = {
        "origin": "AI_EDITOR",
        "resourceType": "AGENTIC_REQUEST",
        "isEmailRequired": "true",
    }
    if auth.profile_arn:
        params["profileArn"] = auth.profile_arn
    url = f"https://q.{_usage_region(account)}.amazonaws.com/getUsageLimits?{urlencode(params)}"
    headers = get_kiro_headers(auth, token)
    headers.pop("x-amz-target", None)
    headers["Accept"] = "application/json"

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(url, headers=headers)
    response.raise_for_status()
    payload = response.json()

    breakdowns = payload.get("usageBreakdownList") or []
    breakdown = breakdowns[0] if breakdowns else {}
    subscription = payload.get("subscriptionInfo") or {}
    overage = payload.get("overageConfiguration") or {}
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
