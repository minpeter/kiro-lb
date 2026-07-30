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

"""Private operations dashboard for kiro-lb.

The dashboard deliberately stores metadata only: no prompts, completions, API
keys, refresh tokens, or OAuth credentials are written to its SQLite database.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse

from kiro.account_manager import account_label, account_routing_state
from kiro.config import FALLBACK_MODELS
from kiro.accounts_admin import register_account
from kiro.usage import fetch_account_usage

router = APIRouter(tags=["dashboard"])

_DATA_DIR = Path(os.getenv("DASHBOARD_DATA_DIR", "data"))
_DB_PATH = _DATA_DIR / "dashboard.sqlite3"
_STATIC_DIR = Path(__file__).parent / "static"
_COOKIE = "kiro_lb_session"
_SESSION_TTL_SECONDS = 12 * 60 * 60
_sessions: dict[str, float] = {}


def _db() -> sqlite3.Connection:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_dashboard_store() -> None:
    with _db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS request_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                route TEXT NOT NULL,
                model TEXT,
                status_code INTEGER NOT NULL,
                latency_ms INTEGER NOT NULL
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_request_logs_created_at ON request_logs(created_at)")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                key_prefix TEXT NOT NULL,
                salt BLOB NOT NULL,
                key_hash BLOB NOT NULL,
                created_at INTEGER NOT NULL,
                revoked_at INTEGER
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_prefix ON api_keys(key_prefix)")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS account_usage (
                account_id TEXT PRIMARY KEY,
                subscription_title TEXT,
                subscription_type TEXT,
                resource_type TEXT,
                current_usage REAL,
                usage_limit REAL,
                usage_percent REAL,
                unit TEXT,
                next_date_reset TEXT,
                days_until_reset REAL,
                overage_status TEXT,
                overage_used REAL,
                updated_at INTEGER NOT NULL,
                error TEXT
            )"""
        )
        # Additive migration for stores created before overage tracking existed.
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(account_usage)")}
        for column, ddl in (("overage_status", "TEXT"), ("overage_used", "REAL")):
            if column not in existing:
                conn.execute(f"ALTER TABLE account_usage ADD COLUMN {column} {ddl}")


def record_request(route: str, model: str | None, status_code: int, latency_ms: int) -> None:
    """Persist non-sensitive data-plane request metadata only."""
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO request_logs(created_at, route, model, status_code, latency_ms) VALUES (?, ?, ?, ?, ?)",
                (int(time.time()), route, model, status_code, latency_ms),
            )
    except Exception:
        # Observability must never break the proxy data plane.
        pass


def _password() -> str:
    return os.getenv("DASHBOARD_PASSWORD", "")


def _hash_api_key(value: str, salt: bytes) -> bytes:
    return hashlib.scrypt(value.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)


def create_data_api_key(name: str) -> tuple[str, dict[str, Any]]:
    """Create a one-time-visible API key; only an scrypt verifier is persisted."""
    raw_key = "klb_" + secrets.token_urlsafe(32)
    key_id = secrets.token_hex(12)
    salt = secrets.token_bytes(16)
    created_at = int(time.time())
    key_prefix = raw_key[:12]
    with _db() as conn:
        conn.execute(
            "INSERT INTO api_keys(id, name, key_prefix, salt, key_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (key_id, name.strip() or "Unnamed key", key_prefix, salt, _hash_api_key(raw_key, salt), created_at),
        )
    return raw_key, {"id": key_id, "name": name.strip() or "Unnamed key", "prefix": key_prefix, "createdAt": created_at, "revokedAt": None}


def verify_data_api_key(value: str) -> bool:
    """Accept the legacy env key or an active, hashed dashboard-managed key."""
    legacy = os.getenv("PROXY_API_KEY", "")
    if legacy and hmac.compare_digest(value, legacy):
        return True
    if not value.startswith("klb_"):
        return False
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT salt, key_hash FROM api_keys WHERE key_prefix = ? AND revoked_at IS NULL", (value[:12],)
            ).fetchall()
        return any(hmac.compare_digest(_hash_api_key(value, row["salt"]), row["key_hash"]) for row in rows)
    except Exception:
        return False


def list_data_api_keys() -> list[dict[str, Any]]:
    with _db() as conn:
        rows = conn.execute("SELECT id, name, key_prefix, created_at, revoked_at FROM api_keys ORDER BY created_at DESC").fetchall()
    return [{"id": r["id"], "name": r["name"], "prefix": r["key_prefix"], "createdAt": r["created_at"], "revokedAt": r["revoked_at"]} for r in rows]


def revoke_data_api_key(key_id: str) -> bool:
    with _db() as conn:
        result = conn.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (int(time.time()), key_id))
    return result.rowcount > 0


def _authenticated(request: Request) -> bool:
    token = request.cookies.get(_COOKIE)
    if not token:
        return False
    expires_at = _sessions.get(token, 0)
    if expires_at <= time.time():
        _sessions.pop(token, None)
        return False
    return True


def _require_auth(request: Request) -> None:
    if not _authenticated(request):
        raise HTTPException(status_code=401, detail="Dashboard authentication required")


def _cached_usage(account_id: str) -> dict[str, Any] | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM account_usage WHERE account_id = ?", (account_id,)).fetchone()
    if not row:
        return None
    return {
        "subscriptionTitle": row["subscription_title"], "subscriptionType": row["subscription_type"],
        "resourceType": row["resource_type"], "currentUsage": row["current_usage"],
        "usageLimit": row["usage_limit"], "usagePercent": row["usage_percent"], "unit": row["unit"],
        "nextDateReset": row["next_date_reset"], "daysUntilReset": row["days_until_reset"],
        "overageStatus": row["overage_status"], "overageUsed": row["overage_used"],
        "updatedAt": row["updated_at"], "error": row["error"],
    }


async def refresh_account_usage(account: Any) -> dict[str, Any]:
    """Refresh one account and persist only the normalized, non-secret summary."""
    updated_at = int(time.time())
    try:
        usage = await fetch_account_usage(account)
        with _db() as conn:
            conn.execute(
                """INSERT INTO account_usage(account_id, subscription_title, subscription_type, resource_type, current_usage, usage_limit, usage_percent, unit, next_date_reset, days_until_reset, overage_status, overage_used, updated_at, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(account_id) DO UPDATE SET subscription_title=excluded.subscription_title, subscription_type=excluded.subscription_type, resource_type=excluded.resource_type, current_usage=excluded.current_usage, usage_limit=excluded.usage_limit, usage_percent=excluded.usage_percent, unit=excluded.unit, next_date_reset=excluded.next_date_reset, days_until_reset=excluded.days_until_reset, overage_status=excluded.overage_status, overage_used=excluded.overage_used, updated_at=excluded.updated_at, error=NULL""",
                (account.id, usage["subscriptionTitle"], usage["subscriptionType"], usage["resourceType"], usage["currentUsage"], usage["usageLimit"], usage["usagePercent"], usage["unit"], str(usage["nextDateReset"] or ""), usage["daysUntilReset"], usage["overageStatus"], usage["overageUsed"], updated_at),
            )
        return {**usage, "updatedAt": updated_at, "error": None}
    except Exception as exc:
        error = str(exc)[:240]
        with _db() as conn:
            conn.execute(
                """INSERT INTO account_usage(account_id, updated_at, error) VALUES (?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET updated_at=excluded.updated_at, error=excluded.error""",
                (account.id, updated_at, error),
            )
        return {"updatedAt": updated_at, "error": error}


async def refresh_all_account_usage(manager: Any) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for account_id, account in list(manager._accounts.items()):
        # Pool accounts initialize lazily; prepare them so quota is reportable.
        if account.auth_manager is None:
            try:
                await manager._initialize_account(account_id)
            except Exception:
                pass
        results.append(await refresh_account_usage(manager._accounts[account_id]))
    return results


def _account_view(account: Any) -> dict[str, Any]:
    now = time.time()
    cooldown_seconds = max(0, int(account.last_failure_time - now)) if account.last_failure_time else 0
    # The failure counter no longer covers every exclusion: rate limits and
    # quota exhaustion deliberately bypass the Circuit Breaker, so the routing
    # state is what tells an operator whether this account is serving traffic.
    routing_state, eligible_in = account_routing_state(account, now)
    return {
        "id": account_label(account.id),
        "initialized": account.auth_manager is not None,
        "routingState": routing_state,
        "eligibleInSeconds": eligible_in,
        "failures": account.failures,
        "cooldownSeconds": cooldown_seconds,
        "modelsCachedAt": int(account.models_cached_at or 0),
        "requests": account.stats.total_requests,
        "successfulRequests": account.stats.successful_requests,
        "failedRequests": account.stats.failed_requests,
        "usage": _cached_usage(account.id),
    }


@router.post("/api/dashboard/login")
async def dashboard_login(request: Request, response: Response) -> dict[str, bool]:
    password = _password()
    if not password:
        raise HTTPException(status_code=503, detail="DASHBOARD_PASSWORD is not configured")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Expected JSON body") from exc
    candidate = str(payload.get("password", ""))
    if not hmac.compare_digest(candidate, password):
        raise HTTPException(status_code=401, detail="Invalid password")
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + _SESSION_TTL_SECONDS
    response.set_cookie(_COOKIE, token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=_SESSION_TTL_SECONDS)
    return {"ok": True}


@router.post("/api/dashboard/logout")
async def dashboard_logout(request: Request, response: Response) -> dict[str, bool]:
    _sessions.pop(request.cookies.get(_COOKIE, ""), None)
    response.delete_cookie(_COOKIE)
    return {"ok": True}


@router.get("/api/dashboard/keys")
async def dashboard_list_keys(request: Request) -> dict[str, list[dict[str, Any]]]:
    _require_auth(request)
    return {"apiKeys": list_data_api_keys()}


@router.post("/api/dashboard/keys")
async def dashboard_create_key(request: Request) -> dict[str, Any]:
    _require_auth(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Expected JSON body") from exc
    raw_key, metadata = create_data_api_key(str(payload.get("name", "")))
    return {"apiKey": raw_key, "metadata": metadata}


@router.delete("/api/dashboard/keys/{key_id}")
async def dashboard_revoke_key(key_id: str, request: Request) -> dict[str, bool]:
    _require_auth(request)
    if not revoke_data_api_key(key_id):
        raise HTTPException(status_code=404, detail="Active API key not found")
    return {"ok": True}


@router.get("/api/dashboard/overview")
async def dashboard_overview(request: Request) -> dict[str, Any]:
    _require_auth(request)
    manager = request.app.state.account_manager
    with _db() as conn:
        totals = conn.execute(
            "SELECT COUNT(*) AS requests, COALESCE(SUM(status_code BETWEEN 200 AND 399), 0) AS successes, COALESCE(AVG(latency_ms), 0) AS avg_latency FROM request_logs WHERE created_at >= ?",
            (int(time.time()) - 86400,),
        ).fetchone()
    accounts = list(manager._accounts.values())
    return {
        "proxy": {"status": "healthy", "uptimeSeconds": int(time.time() - request.app.state.started_at)},
        "requests24h": totals["requests"],
        "successes24h": totals["successes"],
        "averageLatencyMs": round(totals["avg_latency"]),
        "accounts": {"total": len(accounts), "initialized": sum(a.auth_manager is not None for a in accounts)},
        "models": len(manager.get_all_available_models() or FALLBACK_MODELS),
        "reasoning": "Native upstream reasoning is not currently exposed by Kiro; kiro-lb never fabricates it.",
    }


@router.post("/api/dashboard/accounts")
async def dashboard_register_account(request: Request) -> dict[str, Any]:
    _require_auth(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Expected JSON body") from exc
    try:
        result = await register_account(request.app.state.account_manager, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Account registration failed: {exc}") from exc
    # Populate quota immediately so the new account is not blank in the UI.
    manager = request.app.state.account_manager
    account = manager._accounts.get(result.pop("accountKey", ""))
    if account is not None and account.auth_manager is not None:
        await refresh_account_usage(account)
    return result


@router.post("/api/dashboard/accounts/refresh-usage")
async def dashboard_refresh_usage(request: Request) -> dict[str, Any]:
    _require_auth(request)
    return {"accounts": await refresh_all_account_usage(request.app.state.account_manager)}


@router.get("/api/dashboard/accounts")
async def dashboard_accounts(request: Request) -> dict[str, list[dict[str, Any]]]:
    _require_auth(request)
    return {"accounts": [_account_view(account) for account in request.app.state.account_manager._accounts.values()]}


@router.get("/api/dashboard/request-rate")
async def dashboard_request_rate(request: Request, window: int = 900, bucket: int = 15) -> dict[str, Any]:
    _require_auth(request)
    bucket = max(5, min(bucket, 300))
    window = max(bucket, min(window, 6 * 60 * 60))
    return request.app.state.account_manager.request_rate_series(window, bucket)


@router.get("/api/dashboard/models")
async def dashboard_models(request: Request) -> dict[str, list[dict[str, str]]]:
    _require_auth(request)
    models = request.app.state.account_manager.get_all_available_models() or FALLBACK_MODELS
    return {"models": [{"id": item.get("modelId", item) if isinstance(item, dict) else item} for item in models]}


@router.get("/api/dashboard/request-logs")
async def dashboard_request_logs(request: Request, limit: int = 25, offset: int = 0) -> dict[str, Any]:
    _require_auth(request)
    limit = max(1, min(limit, 250))
    offset = max(0, offset)
    with _db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM request_logs").fetchone()[0]
        rows = conn.execute(
            "SELECT created_at, route, model, status_code, latency_ms FROM request_logs"
            " ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return {
        "logs": [dict(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "hasMore": offset + len(rows) < total,
    }


@router.get("/")
async def dashboard_page() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")
