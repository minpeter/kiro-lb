# -*- coding: utf-8 -*-
"""Private operations dashboard for kiro-lb.

The dashboard deliberately stores metadata only: no prompts, completions, API
keys, refresh tokens, or OAuth credentials are written to its SQLite database.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import math
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from loguru import logger

from kiro import (
    agent_mode,
    concurrency,
    endpoint_probe,
    endpoint_settings,
    gateway_tunables,
    model_costs,
    prompt_filter,
    proxy_chain,
)
from kiro.account_manager import account_label, account_routing_state
from kiro.accounts_admin import (
    AccountConflictError,
    AccountNotFoundError,
    DirectoryBackedAccountError,
    LastAccountError,
    account_id_for_entry,
    is_account_deletable,
    register_account,
    remove_account,
    set_account_enabled,
)
from kiro.config import (
    APP_VERSION,
    FALLBACK_MODELS,
    HIDDEN_MODELS,
    MODEL_ALIASES,
    RATE_OBSERVATION_RETENTION_DAYS,
    REQUEST_LOG_RETENTION_DAYS,
)
from kiro.device_login import (
    DeviceLoginError,
    discard_flow,
    internal_credentials,
    poll_device_login,
    resolve_provider,
    start_device_login,
)
from kiro.endpoints import KIRO_ENDPOINTS
from kiro.metrics import CONTENT_TYPE as METRICS_CONTENT_TYPE
from kiro.metrics import render_metrics
from kiro.model_resolver import normalize_model_name
from kiro.store import connection as _db
from kiro.store import database_path
from kiro.store import initialize as initialize_shared_store
from kiro.tunables import InvalidSetting
from kiro.usage import fetch_account_usage
from kiro.usage_tracking import (
    ROOT_KEY_ID,
    UNKNOWN_ACCOUNT_ID,
    drain_pending_usage,
    restore_pending_usage,
)

router = APIRouter(tags=["dashboard"])

_STATIC_DIR = Path(__file__).parent / "static"
_COOKIE = "kiro_lb_session"
_SESSION_TTL_SECONDS = 12 * 60 * 60


def _proxy_api_key() -> str:
    """Return the legacy root key without caching environment state."""
    return os.getenv("PROXY_API_KEY", "")


def initialize_dashboard_store() -> None:
    initialize_shared_store()
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
            """CREATE TABLE IF NOT EXISTS request_metric_rollups (
                route TEXT NOT NULL, model TEXT NOT NULL, status_code INTEGER NOT NULL,
                requests INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(route, model, status_code)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS request_latency_rollups (
                route TEXT NOT NULL, model TEXT NOT NULL,
                requests INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(route, model)
            )"""
        )
        conn.execute("CREATE TABLE IF NOT EXISTS dashboard_migrations (name TEXT PRIMARY KEY)")
        if not conn.execute("SELECT 1 FROM dashboard_migrations WHERE name = 'request_rollups_v1'").fetchone():
            conn.execute(
                "INSERT INTO request_metric_rollups(route, model, status_code, requests)"
                " SELECT route, COALESCE(model, ''), status_code, COUNT(*)"
                " FROM request_logs GROUP BY route, COALESCE(model, ''), status_code"
            )
            conn.execute(
                "INSERT INTO request_latency_rollups(route, model, requests, latency_ms)"
                " SELECT route, COALESCE(model, ''), COUNT(*), COALESCE(SUM(latency_ms), 0) FROM request_logs"
                " WHERE status_code BETWEEN 200 AND 399 GROUP BY route, COALESCE(model, '')"
            )
            conn.execute("INSERT INTO dashboard_migrations(name) VALUES ('request_rollups_v1')")
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS rollup_request_log AFTER INSERT ON request_logs BEGIN
                INSERT INTO request_metric_rollups(route, model, status_code, requests)
                VALUES (NEW.route, COALESCE(NEW.model, ''), NEW.status_code, 1)
                ON CONFLICT(route, model, status_code) DO UPDATE SET requests = requests + 1;
                INSERT INTO request_latency_rollups(route, model, requests, latency_ms)
                SELECT NEW.route, COALESCE(NEW.model, ''), 1, NEW.latency_ms
                WHERE NEW.status_code BETWEEN 200 AND 399
                ON CONFLICT(route, model) DO UPDATE SET requests = requests + 1,
                    latency_ms = latency_ms + excluded.latency_ms;
            END"""
        )
        # Rate observations back the inferred rate limit shown on the dashboard.
        # Only the RPM at each upstream verdict is kept, which is what the
        # estimate needs; the high-resolution chart ring stays in memory.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS rate_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                observed_at REAL NOT NULL,
                rpm INTEGER NOT NULL,
                rejected INTEGER NOT NULL,
                outcome TEXT NOT NULL DEFAULT 'success'
            )"""
        )
        # Additive migration for stores created before outcomes were recorded.
        rate_columns = {row["name"] for row in conn.execute("PRAGMA table_info(rate_observations)")}
        if "outcome" not in rate_columns:
            conn.execute("ALTER TABLE rate_observations ADD COLUMN outcome TEXT NOT NULL DEFAULT 'success'")
            conn.execute("UPDATE rate_observations SET outcome = 'rate_limited' WHERE rejected = 1")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rate_observations_account ON rate_observations(account_id, observed_at)"
        )
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
        # Per-key, per-model token totals. Cumulative rather than per-request so
        # the table stays bounded by keys x models instead of traffic volume.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS key_model_usage (
                key_id TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                requests INTEGER NOT NULL DEFAULT 0,
                generation_ms INTEGER NOT NULL DEFAULT 0,
                timed_completion_tokens INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (key_id, model)
            )"""
        )
        # Additive migration for stores created before generation time was tracked.
        # Existing rows keep 0, which reads as "no timing yet" rather than as an
        # instant response: a throughput consumer has to divide by it, so the
        # zero must be skipped, not treated as a measurement.
        usage_columns = {row["name"] for row in conn.execute("PRAGMA table_info(key_model_usage)")}
        for column in ("generation_ms", "timed_completion_tokens"):
            if column not in usage_columns:
                conn.execute(f"ALTER TABLE key_model_usage ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
        # Detail columns for the request log. All nullable: old rows have no
        # detail, and text is only present when capture is enabled.
        log_columns = {row["name"] for row in conn.execute("PRAGMA table_info(request_logs)")}
        for column, ddl in (
            ("client_ip", "TEXT"),
            ("user_agent", "TEXT"),
            ("api_key_name", "TEXT"),
            ("input_tokens", "INTEGER"),
            ("output_tokens", "INTEGER"),
            ("credits", "REAL"),
        ):
            if column not in log_columns:
                conn.execute(f"ALTER TABLE request_logs ADD COLUMN {column} {ddl}")
        # Token totals per (key, account, model). This is the grain the two older
        # tables could not express between them: key_model_usage knows tokens but
        # not which account served them, and the account request counters know the
        # account but hold no tokens, so "tokens per account per model" had no
        # answer and could not be backfilled - request_logs carries no account_id.
        #
        # Cumulative like key_model_usage, so the row count stays bounded by
        # keys x accounts x models rather than by traffic. Every narrower view
        # (per key, per account, per model) is a projection of this one, which is
        # why no additional counter table is introduced alongside it.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS account_model_usage (
                key_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                requests INTEGER NOT NULL DEFAULT 0,
                generation_ms INTEGER NOT NULL DEFAULT 0,
                timed_completion_tokens INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (key_id, account_id, model)
            )"""
        )
        # Aggregations filter by account or by model, never by key alone, so the
        # primary key's leading column is the wrong index for them.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_account_model_usage_account ON account_model_usage(account_id, model)"
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS account_usage (
                account_id TEXT PRIMARY KEY,
                email TEXT,
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
        for column, ddl in (("overage_status", "TEXT"), ("overage_used", "REAL"), ("email", "TEXT")):
            if column not in existing:
                conn.execute(f"ALTER TABLE account_usage ADD COLUMN {column} {ddl}")

        _merge_unnormalized_usage_models(conn)


def _merge_unnormalized_usage_models(conn: sqlite3.Connection) -> int:
    """Fold rows stored under a client spelling into the normalized model.

    `record_token_usage` normalizes before writing, but rows recorded before that
    keep whatever the client sent: `claude-sonnet-4-5` sat beside
    `claude-sonnet-4.5` as a separate model, so one model's totals were split
    across two rows and it appeared twice in the dashboard.

    Totals are added, not overwritten. Dropping the old row instead would lose
    the tokens it accounted for, and taking the larger of the two would silently
    understate consumption.
    """
    merged = 0
    rows = conn.execute(
        "SELECT key_id, model, prompt_tokens, completion_tokens, requests, generation_ms,"
        " timed_completion_tokens, updated_at FROM key_model_usage"
    ).fetchall()
    for row in rows:
        canonical = normalize_model_name(row["model"]) or row["model"]
        if canonical == row["model"]:
            continue
        conn.execute(
            """INSERT INTO key_model_usage(key_id, model, prompt_tokens, completion_tokens, requests, generation_ms, timed_completion_tokens, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key_id, model) DO UPDATE SET
                prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                completion_tokens = completion_tokens + excluded.completion_tokens,
                requests = requests + excluded.requests,
                generation_ms = generation_ms + excluded.generation_ms,
                timed_completion_tokens = timed_completion_tokens + excluded.timed_completion_tokens,
                updated_at = MAX(updated_at, excluded.updated_at)""",
            (
                row["key_id"],
                canonical,
                row["prompt_tokens"],
                row["completion_tokens"],
                row["requests"],
                row["generation_ms"],
                row["timed_completion_tokens"],
                row["updated_at"],
            ),
        )
        conn.execute("DELETE FROM key_model_usage WHERE key_id = ? AND model = ?", (row["key_id"], row["model"]))
        merged += 1
    if merged:
        logger.info("Merged {} usage row(s) stored under a non-normalized model name", merged)
    return merged


def prune_request_logs() -> int:
    """Drop request-log rows past the retention horizon.

    The table is append-only on the data path, so without pruning the 24h
    overview aggregate slows as history accumulates: 0.85ms at 10k rows versus
    19.8ms at 1M measured on this deployment.

    The rollups are deliberately left alone. They feed kiro_lb_requests_total,
    a Prometheus counter, so they must only ever increase: deleting from them
    here would make the counter fall on every retention pass and report a reset
    that never happened. Surviving the pruning is why they exist, and the only
    place they are cleared is the explicit operator wipe in data/clear.
    """
    cutoff = int(time.time()) - REQUEST_LOG_RETENTION_DAYS * 86400
    try:
        with _db() as conn:
            return conn.execute("DELETE FROM request_logs WHERE created_at < ?", (cutoff,)).rowcount
    except Exception:
        return 0


def record_rate_observations(rows: list[tuple[str, float, int, int, str]]) -> bool:
    """Persist (account_id, observed_at, rpm, rejected, outcome) rate samples."""
    if not rows:
        return True
    try:
        with _db() as conn:
            conn.executemany(
                "INSERT INTO rate_observations(account_id, observed_at, rpm, rejected, outcome) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        return True
    except Exception as exc:
        logger.error("Failed to persist rate observations: {}", exc)
        return False


def load_rate_observations(since: float) -> list[tuple[str, float, int, int, str]]:
    try:
        with _db() as conn:
            return [
                (row["account_id"], row["observed_at"], row["rpm"], row["rejected"], row["outcome"])
                for row in conn.execute(
                    "SELECT account_id, observed_at, rpm, rejected, outcome FROM rate_observations"
                    " WHERE observed_at >= ? ORDER BY observed_at",
                    (since,),
                )
            ]
    except Exception:
        return []


def prune_rate_observations() -> int:
    cutoff = time.time() - RATE_OBSERVATION_RETENTION_DAYS * 86400
    try:
        with _db() as conn:
            return conn.execute("DELETE FROM rate_observations WHERE observed_at < ?", (cutoff,)).rowcount
    except Exception:
        return 0


def flush_key_model_usage() -> int:
    """Fold accumulated token counts into the store.

    One drained batch writes both tables inside a single transaction. The per-key
    table is the sum of the per-account one over accounts, and that only holds if
    neither can be written without the other: a partial flush would leave the two
    views permanently disagreeing with no way to tell which is short.
    """
    pending = drain_pending_usage()
    if not pending:
        return 0
    now = int(time.time())
    try:
        with _db() as conn:
            conn.executemany(
                """INSERT INTO key_model_usage(key_id, model, prompt_tokens, completion_tokens, requests, generation_ms, timed_completion_tokens, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key_id, model) DO UPDATE SET
                    prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                    completion_tokens = completion_tokens + excluded.completion_tokens,
                    requests = requests + excluded.requests,
                    generation_ms = generation_ms + excluded.generation_ms,
                    timed_completion_tokens = timed_completion_tokens + excluded.timed_completion_tokens,
                    updated_at = excluded.updated_at""",
                [
                    (key_id, model, prompt, completion, requests, generation_ms, timed, now)
                    for key_id, _account_id, model, prompt, completion, requests, generation_ms, timed in pending
                ],
            )
            conn.executemany(
                """INSERT INTO account_model_usage(key_id, account_id, model, prompt_tokens, completion_tokens, requests, generation_ms, timed_completion_tokens, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key_id, account_id, model) DO UPDATE SET
                    prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                    completion_tokens = completion_tokens + excluded.completion_tokens,
                    requests = requests + excluded.requests,
                    generation_ms = generation_ms + excluded.generation_ms,
                    timed_completion_tokens = timed_completion_tokens + excluded.timed_completion_tokens,
                    updated_at = excluded.updated_at""",
                [
                    (key_id, account_id, model, prompt, completion, requests, generation_ms, timed, now)
                    for key_id, account_id, model, prompt, completion, requests, generation_ms, timed in pending
                ],
            )
        return len(pending)
    except Exception as exc:
        restore_pending_usage(pending)
        logger.error("Failed to flush token usage; restored pending batch: {}", exc)
        return 0


def _usage_view(row: Any, label_column: str, label_key: str) -> dict[str, Any]:
    """Shape one cumulative token row for the dashboard.

    Shared by the per-key and per-account views so the throughput rule below is
    stated once. Duplicating it is how the 82,752 tok/s reading got shipped.
    """
    generation_ms = row["generation_ms"] or 0
    completion = row["completion_tokens"]
    timed_completion = row["timed_completion_tokens"] or 0
    return {
        label_key: row[label_column],
        "promptTokens": row["prompt_tokens"],
        "completionTokens": completion,
        "totalTokens": row["prompt_tokens"] + completion,
        "requests": row["requests"],
        "generationSeconds": generation_ms / 1000,
        # Only the tokens that were also timed may divide the duration. Using the
        # full total mixes in rows recorded before timing existed and reported
        # 82,752 tok/s on the live store. Absent rather than 0 when nothing has
        # been timed yet.
        "tokensPerSecond": (timed_completion / (generation_ms / 1000)) if generation_ms > 0 else None,
        "updatedAt": row["updated_at"],
    }


def key_model_usage() -> dict[str, list[dict[str, Any]]]:
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT key_id, model, prompt_tokens, completion_tokens, requests, generation_ms,"
                " timed_completion_tokens, updated_at"
                " FROM key_model_usage ORDER BY prompt_tokens + completion_tokens DESC"
            ).fetchall()
    except Exception:
        return {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["key_id"], []).append(_usage_view(row, "model", "model"))
    return grouped


def account_model_usage() -> dict[str, list[dict[str, Any]]]:
    """Cumulative tokens per account, broken down by model.

    Summed over keys: the operational question is what an account spent, and which
    key drove it is already answerable from ``key_model_usage``. Keeping the key
    axis here would multiply the rows an operator has to read for no extra fact.
    """
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT account_id, model, SUM(prompt_tokens) AS prompt_tokens,"
                " SUM(completion_tokens) AS completion_tokens, SUM(requests) AS requests,"
                " SUM(generation_ms) AS generation_ms,"
                " SUM(timed_completion_tokens) AS timed_completion_tokens,"
                " MAX(updated_at) AS updated_at"
                " FROM account_model_usage GROUP BY account_id, model"
                " ORDER BY SUM(prompt_tokens) + SUM(completion_tokens) DESC"
            ).fetchall()
    except Exception:
        return {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["account_id"], []).append(_usage_view(row, "model", "model"))
    return grouped


def record_request(
    route: str,
    model: str | None,
    status_code: int,
    latency_ms: int,
    *,
    client_ip: str | None = None,
    user_agent: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    credits_spent: float | None = None,
) -> None:
    """Persist request metadata. The store never holds request or response text.

    ``credits`` holds what the upstream reported for this request. Kiro meters
    fractionally, in 0.01 increments, from input and output volume, and publishes
    no token-to-credit rate, so nothing is inferred: when the upstream reports
    nothing, the column stays empty and the dashboard says so.
    """
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO request_logs("
                "created_at, route, model, status_code, latency_ms, "
                "client_ip, user_agent, credits, input_tokens, output_tokens"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(time.time()),
                    route,
                    model,
                    status_code,
                    latency_ms,
                    client_ip,
                    (user_agent or None) and user_agent[:200],
                    credits_spent,
                    input_tokens,
                    output_tokens,
                ),
            )
    except Exception:
        # Observability must never break the proxy data plane.
        pass


def _password() -> str:
    return os.getenv("DASHBOARD_PASSWORD", "")


def _session_token(expires_at: int) -> str:
    payload = str(expires_at)
    signature = hmac.new(_password().encode(), payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def _secure_cookie(request: Request) -> bool:
    configured = os.getenv("DASHBOARD_SECURE_COOKIE")
    if configured is not None:
        return configured.lower() in ("true", "1", "yes")
    forwarded = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    return request.url.scheme == "https" or forwarded == "https"


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
    return raw_key, {
        "id": key_id,
        "name": name.strip() or "Unnamed key",
        "prefix": key_prefix,
        "createdAt": created_at,
        "revokedAt": None,
    }


def identify_data_api_key(value: str) -> str | None:
    """Return the id of the key that matches, or None.

    The legacy environment key answers as ROOT_KEY_ID: it has no row of its own
    but still needs to be attributable in per-key usage.
    """
    legacy = _proxy_api_key()
    if legacy and hmac.compare_digest(value, legacy):
        return ROOT_KEY_ID
    if not value.startswith("klb_"):
        return None
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT id, salt, key_hash FROM api_keys WHERE key_prefix = ? AND revoked_at IS NULL", (value[:12],)
            ).fetchall()
        for row in rows:
            if hmac.compare_digest(_hash_api_key(value, row["salt"]), row["key_hash"]):
                return row["id"]
        return None
    except Exception:
        return None


def verify_data_api_key(value: str) -> bool:
    """Accept the legacy env key or an active, hashed dashboard-managed key."""
    return identify_data_api_key(value) is not None


def list_data_api_keys() -> list[dict[str, Any]]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, name, key_prefix, created_at, revoked_at FROM api_keys ORDER BY created_at DESC"
        ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "prefix": r["key_prefix"],
            "createdAt": r["created_at"],
            "revokedAt": r["revoked_at"],
        }
        for r in rows
    ]


def revoke_data_api_key(key_id: str) -> bool:
    with _db() as conn:
        result = conn.execute(
            "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (int(time.time()), key_id)
        )
    return result.rowcount > 0


def delete_data_api_key(key_id: str) -> bool:
    """Remove a key outright, along with the usage rows that name it.

    Leaving the usage behind would show traffic attributed to a key that no
    longer exists, which is how the account deletion path handles it too.
    """
    with _db() as conn:
        result = conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
        if result.rowcount:
            conn.execute("DELETE FROM key_model_usage WHERE key_id = ?", (key_id,))
            conn.execute("DELETE FROM account_model_usage WHERE key_id = ?", (key_id,))
    return result.rowcount > 0


def rename_data_api_key(key_id: str, name: str) -> bool:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("name cannot be empty")
    if len(cleaned) > 80:
        raise ValueError("name must be 80 characters or fewer")
    with _db() as conn:
        result = conn.execute("UPDATE api_keys SET name = ? WHERE id = ?", (cleaned, key_id))
    return result.rowcount > 0


def _authenticated(request: Request) -> bool:
    token = request.cookies.get(_COOKIE)
    if not token:
        return False
    try:
        raw_expiry, supplied = token.split(".", 1)
        expected = _session_token(int(raw_expiry)).split(".", 1)[1]
        if int(raw_expiry) <= time.time() or not hmac.compare_digest(supplied, expected):
            return False
    except (TypeError, ValueError):
        return False
    return True


def _require_auth(request: Request) -> None:
    if not _authenticated(request):
        raise HTTPException(status_code=401, detail="Dashboard authentication required")


def _account_emails() -> dict[str, str]:
    """Map internal account id to email, for labelling usage rows.

    One query rather than a ``_cached_usage`` call per account: the usage route
    already returns a row per account and model, and the N+1 would grow with the
    pool.
    """
    try:
        with _db() as conn:
            rows = conn.execute("SELECT account_id, email FROM account_usage WHERE email IS NOT NULL").fetchall()
    except Exception:
        return {}
    return {row["account_id"]: row["email"] for row in rows}


def _cached_usage(account_id: str) -> dict[str, Any] | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM account_usage WHERE account_id = ?", (account_id,)).fetchone()
    if not row:
        return None
    return {
        "email": row["email"],
        "subscriptionTitle": row["subscription_title"],
        "subscriptionType": row["subscription_type"],
        "resourceType": row["resource_type"],
        "currentUsage": row["current_usage"],
        "usageLimit": row["usage_limit"],
        "usagePercent": row["usage_percent"],
        "unit": row["unit"],
        "nextDateReset": row["next_date_reset"],
        "daysUntilReset": row["days_until_reset"],
        "overageStatus": row["overage_status"],
        "overageUsed": row["overage_used"],
        "updatedAt": row["updated_at"],
        "error": row["error"],
    }


#: Upper bound on a stored poll error. The column is operator-facing text in a
#: table cell, not a log line: httpx's own HTTPStatusError message is 188 chars
#: across two lines and made the accounts table render wider than the viewport.
_MAX_USAGE_ERROR_CHARS = 120


def _summarize_usage_error(exc: Exception) -> str:
    """Reduce a failed usage poll to one short, single-line operator message.

    Three things are deliberately stripped. The embedded newline, because the
    dashboard renders this string in a table cell. The refresh/usage URL that
    httpx interpolates, because it is fixed infrastructure the operator cannot
    act on. And the length, because an unbounded upstream string is a layout bug
    waiting to happen in any consumer.
    """
    from kiro.account_errors import CredentialDeadError

    if isinstance(exc, CredentialDeadError):
        return f"credential rejected by the auth host (HTTP {exc.status_code}); re-login required"
    if isinstance(exc, httpx.HTTPStatusError):
        # Status plus reason only. The verdict is the status code; the URL and the
        # MDN link httpx appends carry no account-specific information.
        return f"upstream returned HTTP {exc.response.status_code} for the usage query"
    if isinstance(exc, httpx.TimeoutException):
        return "usage query timed out"
    if isinstance(exc, httpx.RequestError):
        return f"usage query failed to reach the upstream ({type(exc).__name__})"
    collapsed = " ".join(str(exc).split())
    if not collapsed:
        collapsed = type(exc).__name__
    if len(collapsed) > _MAX_USAGE_ERROR_CHARS:
        return collapsed[: _MAX_USAGE_ERROR_CHARS - 1].rstrip() + "…"
    return collapsed


async def refresh_account_usage(account: Any) -> dict[str, Any]:
    """Refresh one account and persist only the normalized, non-secret summary."""
    updated_at = int(time.time())
    try:
        usage = await fetch_account_usage(account)
        with _db() as conn:
            conn.execute(
                """INSERT INTO account_usage(account_id, email, subscription_title, subscription_type, resource_type, current_usage, usage_limit, usage_percent, unit, next_date_reset, days_until_reset, overage_status, overage_used, updated_at, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(account_id) DO UPDATE SET email=excluded.email, subscription_title=excluded.subscription_title, subscription_type=excluded.subscription_type, resource_type=excluded.resource_type, current_usage=excluded.current_usage, usage_limit=excluded.usage_limit, usage_percent=excluded.usage_percent, unit=excluded.unit, next_date_reset=excluded.next_date_reset, days_until_reset=excluded.days_until_reset, overage_status=excluded.overage_status, overage_used=excluded.overage_used, updated_at=excluded.updated_at, error=NULL""",
                (
                    account.id,
                    usage["email"],
                    usage["subscriptionTitle"],
                    usage["subscriptionType"],
                    usage["resourceType"],
                    usage["currentUsage"],
                    usage["usageLimit"],
                    usage["usagePercent"],
                    usage["unit"],
                    str(usage["nextDateReset"] or ""),
                    usage["daysUntilReset"],
                    usage["overageStatus"],
                    usage["overageUsed"],
                    updated_at,
                ),
            )
        return {**usage, "updatedAt": updated_at, "error": None}
    except Exception as exc:
        error = _summarize_usage_error(exc)
        with _db() as conn:
            conn.execute(
                """INSERT INTO account_usage(account_id, updated_at, error) VALUES (?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET updated_at=excluded.updated_at, error=excluded.error""",
                (account.id, updated_at, error),
            )
        return {"updatedAt": updated_at, "error": error}


def _headroom_from_usage(usage: dict[str, Any]) -> float | None:
    """Convert a usage summary into the unused quota fraction, or None.

    Returns None rather than a guess whenever the reading cannot support the
    ratio: the router treats None as "unknown" and falls back to a neutral
    weight, which is honest, while a fabricated 0.0 or 1.0 would silently bias
    routing on missing telemetry.
    """
    if usage.get("error"):
        return None
    current = usage.get("currentUsage")
    limit = usage.get("usageLimit")
    if not isinstance(current, (int, float)) or not isinstance(limit, (int, float)):
        return None
    if limit <= 0:
        return None
    return max(0.0, min(1.0, 1.0 - (float(current) / float(limit))))


def _reset_at_from_usage(usage: dict[str, Any]) -> float | None:
    """Pull the next quota reset out of a usage summary, or None.

    The upstream value has arrived as a number and as a numeric string, and the
    persisted column is TEXT, so both shapes are accepted. Anything unparseable
    or non-positive is None: a 402 quarantine then falls back to the fixed window
    rather than trusting a fabricated date.
    """
    if usage.get("error"):
        return None
    raw = usage.get("nextDateReset")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        reset_at = float(raw)
    except (TypeError, ValueError):
        return None
    # inf and nan survive float() but break JSON serialization on the accounts
    # route, and neither is a date. An infinite reset would also pin the
    # quarantine to its ceiling instead of falling back to the fixed window.
    if not math.isfinite(reset_at):
        return None
    return reset_at if reset_at > 0 else None


def _overage_enabled_from_usage(usage: dict[str, Any]) -> bool | None:
    """Report whether overage lets the account serve past its allowance.

    Only the two values the upstream actually states are conclusive; "UNKNOWN",
    a missing field, or a failed reading stay None so a spent account is never
    labelled done on a guess.
    """
    if usage.get("error"):
        return None
    status = usage.get("overageStatus")
    if not isinstance(status, str):
        return None
    normalized = status.strip().upper()
    if normalized == "ENABLED":
        return True
    if normalized == "DISABLED":
        return False
    return None


def _apply_routing_weight(manager: Any, account_id: str, usage: dict[str, Any]) -> None:
    """Push a fresh usage reading into the router's selection weight.

    Every path that polls quota must go through here. A poll that updated only
    the dashboard row would leave selection weighting the account on whatever it
    last believed, which for a newly registered account is nothing at all.
    Failures are contained: routing weight is an optimization, and losing it must
    never fail the poll or the registration that triggered it.
    """
    try:
        manager.set_quota_headroom(account_id, _headroom_from_usage(usage))
        manager.set_quota_period(account_id, _reset_at_from_usage(usage), _overage_enabled_from_usage(usage))
    except Exception as exc:
        logger.warning("Failed to update routing weight for {}: {}", account_label(account_id), exc)


async def prime_registered_account_usage(manager: Any, result: dict[str, Any]) -> dict[str, Any]:
    """Poll quota for a freshly registered account and drop the internal key.

    `register_account` returns the raw account ID (a credential path or token
    hash) so the caller can find the new pool entry. That key must never reach
    the client, and every registration route has to poll usage here: the
    periodic refresh is up to USAGE_REFRESH_INTERVAL_SECONDS away, so without
    this the new account shows no email, tier, or usage until then.

    The same poll seeds the routing weight. Without it a new account routes at
    the neutral unknown weight until the next bulk refresh, which understates a
    fresh account that is usually the emptiest one in the pool.
    """
    account_id = result.pop("accountKey", "")
    account = manager._accounts.get(account_id)
    if account is not None and account.auth_manager is not None:
        usage = await refresh_account_usage(account)
        _apply_routing_weight(manager, account_id, usage)
    return result


async def refresh_all_account_usage(manager: Any) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for account_id, account in list(manager._accounts.items()):
        # Pool accounts initialize lazily; prepare them so quota is reportable.
        if account.auth_manager is None:
            try:
                await manager._initialize_account(account_id)
            except Exception:
                pass
        usage = await refresh_account_usage(manager._accounts[account_id])
        # Feed routing weight from the same poll. Selection prefers accounts
        # with quota left, so a refresh that updated the dashboard but not the
        # router would leave routing pinned to whatever it last believed.
        _apply_routing_weight(manager, account_id, usage)
        results.append(usage)
    return results


def _account_view(account: Any, *, deletable: bool = False) -> dict[str, Any]:
    now = time.time()
    cooldown_seconds = max(0, int(account.last_failure_time - now)) if account.last_failure_time else 0
    # The failure counter no longer covers every exclusion: rate limits and
    # quota exhaustion deliberately bypass the Circuit Breaker, so the routing
    # state is what tells an operator whether this account is serving traffic.
    routing_state, eligible_in = account_routing_state(account, now)
    return {
        "id": account_label(account.id),
        "deletable": deletable,
        "initialized": account.auth_manager is not None,
        "routingState": routing_state,
        "eligibleInSeconds": eligible_in,
        "failures": account.failures,
        "cooldownSeconds": cooldown_seconds,
        "modelsCachedAt": int(account.models_cached_at or 0),
        "requests": account.stats.total_requests,
        "successfulRequests": account.stats.successful_requests,
        "failedRequests": account.stats.failed_requests,
        # Routing weight input. `usagePercent` is the dashboard's own view of the
        # same quota, but it is the persisted reading; this is what selection
        # actually holds, so a stale or failed refresh is visible as null here
        # instead of being inferred from a row that still looks fresh.
        "quotaHeadroom": getattr(account, "quota_headroom", None),
        # Null rather than 0 when unknown: "no reset date known" and "resets at
        # the epoch" are different facts, and the client renders them differently.
        "quotaResetsAt": getattr(account, "quota_resets_at", 0.0) or None,
        "quotaOverageEnabled": getattr(account, "quota_overage_enabled", None),
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
    token = _session_token(int(time.time()) + _SESSION_TTL_SECONDS)
    response.set_cookie(
        _COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=_secure_cookie(request),
        max_age=_SESSION_TTL_SECONDS,
    )
    return {"ok": True}


@router.post("/api/dashboard/logout")
async def dashboard_logout(request: Request, response: Response) -> dict[str, bool]:
    response.delete_cookie(_COOKIE, httponly=True, samesite="strict", secure=_secure_cookie(request))
    return {"ok": True}


@router.get("/api/dashboard/keys")
async def dashboard_list_keys(request: Request) -> dict[str, list[dict[str, Any]]]:
    _require_auth(request)
    keys: list[dict[str, Any]] = []
    # The legacy environment key authenticates real traffic but has no row, so
    # it is listed as a read-only root entry: usage must be attributable to it,
    # and it cannot be revoked from here because it lives in the environment.
    legacy = _proxy_api_key()
    if legacy:
        keys.append(
            {
                "id": ROOT_KEY_ID,
                "name": "Root key (environment)",
                "prefix": legacy[:4] + "…",
                "createdAt": None,
                "revokedAt": None,
                "readOnly": True,
            }
        )
    keys.extend({**key, "readOnly": False} for key in list_data_api_keys())
    return {"apiKeys": keys}


@router.get("/api/dashboard/keys/usage")
async def dashboard_key_usage(request: Request) -> dict[str, Any]:
    _require_auth(request)
    # Fold in-memory counts first so the response reflects traffic that has not
    # hit the periodic flush yet.
    await asyncio.to_thread(flush_key_model_usage)
    return {"usage": key_model_usage()}


@router.get("/api/dashboard/accounts/usage")
async def dashboard_account_usage(request: Request) -> dict[str, Any]:
    _require_auth(request)
    # Same flush-first rule as the per-key route: both read the tables the flush
    # writes, so without it a just-served request is missing from the answer.
    await asyncio.to_thread(flush_key_model_usage)
    usage = account_model_usage()
    # Internal account ids are credential file paths. Every other account route
    # exposes the hashed label instead, and leaking a path here would also make
    # the response impossible to join against the accounts panel.
    emails = _account_emails()
    grouped: dict[str, Any] = {}
    for account_id, models in usage.items():
        label = account_label(account_id) if account_id != UNKNOWN_ACCOUNT_ID else UNKNOWN_ACCOUNT_ID
        grouped[label] = {
            "email": emails.get(account_id),
            "models": models,
            "totalTokens": sum(entry["totalTokens"] for entry in models),
            "requests": sum(entry["requests"] for entry in models),
        }
    return {"usage": grouped}


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
async def dashboard_delete_key(key_id: str, request: Request) -> dict[str, bool]:
    _require_auth(request)
    if key_id == ROOT_KEY_ID:
        raise HTTPException(status_code=400, detail="The root key is set in the environment and cannot be deleted here")
    if not delete_data_api_key(key_id):
        raise HTTPException(status_code=404, detail="API key not found")
    return {"ok": True}


@router.patch("/api/dashboard/keys/{key_id}")
async def dashboard_rename_key(key_id: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    if key_id == ROOT_KEY_ID:
        raise HTTPException(status_code=400, detail="The root key is set in the environment and cannot be renamed here")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Expected JSON body") from exc
    if not isinstance(payload, dict) or "name" not in payload:
        raise HTTPException(status_code=400, detail='Expected {"name": "..."}')
    try:
        renamed = rename_data_api_key(key_id, payload["name"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not renamed:
        raise HTTPException(status_code=404, detail="API key not found")
    return {"ok": True, "name": payload["name"].strip()}


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
    return await prime_registered_account_usage(request.app.state.account_manager, result)


@router.post("/api/dashboard/accounts/refresh-usage")
async def dashboard_refresh_usage(request: Request) -> dict[str, Any]:
    _require_auth(request)
    return {"accounts": await refresh_all_account_usage(request.app.state.account_manager)}


@router.post("/api/dashboard/accounts/device-login")
async def dashboard_start_device_login(request: Request) -> dict[str, Any]:
    _require_auth(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    try:
        provider = resolve_provider(payload.get("provider", "google"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        flow = await start_device_login(provider)
    except DeviceLoginError as exc:
        raise HTTPException(status_code=502, detail=f"Kiro rejected the login request: {exc}") from exc
    return flow.view()


@router.get("/api/dashboard/accounts/device-login/{flow_id}")
async def dashboard_poll_device_login(flow_id: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    try:
        flow = await poll_device_login(flow_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return flow.view()


@router.post("/api/dashboard/accounts/device-login/{flow_id}/register")
async def dashboard_register_device_login(flow_id: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    try:
        flow = await poll_device_login(flow_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if flow.status != "approved" or not flow.token:
        raise HTTPException(status_code=409, detail=f"Login is {flow.status}, not approved yet")

    refresh_token = flow.token.get("refreshToken")
    if not refresh_token:
        raise HTTPException(status_code=502, detail="Kiro approved the login without a refresh token")

    registration = {
        "type": "internal",
        "id": f"device-{flow.provider.lower()}-{flow.id}",
        "credential": internal_credentials(flow),
    }

    try:
        result = await register_account(request.app.state.account_manager, registration)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        # The token is registered or rejected; either way it must not linger.
        discard_flow(flow_id)

    result["provider"] = flow.provider
    return await prime_registered_account_usage(request.app.state.account_manager, result)


@router.delete("/api/dashboard/accounts/device-login/{flow_id}")
async def dashboard_cancel_device_login(flow_id: str, request: Request) -> dict[str, bool]:
    _require_auth(request)
    discard_flow(flow_id)
    return {"ok": True}


@router.get("/api/dashboard/accounts")
async def dashboard_accounts(request: Request) -> dict[str, list[dict[str, Any]]]:
    _require_auth(request)
    manager = request.app.state.account_manager
    views = [
        {**_account_view(account, deletable=is_account_deletable(manager, account.id)), "enabled": True}
        for account in manager._accounts.values()
    ]

    # Disabled entries are absent from the live pool, so without this they would
    # vanish from the dashboard and could never be switched back on.
    live_ids = set(manager._accounts)
    for entry in getattr(manager, "_credentials_config", []) or []:
        if entry.get("enabled", True):
            continue
        try:
            account_id = account_id_for_entry(entry)
        except Exception:
            continue
        if account_id in live_ids:
            continue
        views.append(
            {
                "id": account_label(account_id),
                "initialized": False,
                "routingState": "disabled",
                "eligibleInSeconds": 0,
                "requests": 0,
                "failures": 0,
                "cooldownSeconds": 0,
                "deletable": True,
                "enabled": False,
                "usage": _cached_usage(account_id),
            }
        )
    return {"accounts": views}


@router.delete("/api/dashboard/accounts/{label}")
async def dashboard_delete_account(label: str, request: Request) -> dict[str, bool]:
    _require_auth(request)

    try:
        await remove_account(request.app.state.account_manager, label)
    except AccountNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AccountConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {"ok": True}


@router.get("/api/dashboard/request-rate")
async def dashboard_request_rate(request: Request, window: int = 900, bucket: int = 15) -> dict[str, Any]:
    _require_auth(request)
    bucket = max(5, min(bucket, 300))
    window = max(bucket, min(window, 6 * 60 * 60))
    return request.app.state.account_manager.request_rate_series(window, bucket)


@router.get("/api/dashboard/endpoints")
async def dashboard_get_endpoints(request: Request) -> dict[str, Any]:
    _require_auth(request)
    settings = endpoint_settings.current()
    return {
        "available": [
            {"key": item.key, "name": item.name, "url": item.url(_probe_region(request))} for item in KIRO_ENDPOINTS
        ],
        "settings": settings.as_dict(),
        "pingRepsMax": endpoint_probe.PING_REPS_MAX,
        "pingRepsDefault": endpoint_probe.PING_REPS_DEFAULT,
    }


@router.put("/api/dashboard/endpoints")
async def dashboard_update_endpoints(request: Request) -> dict[str, Any]:
    _require_auth(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Expected JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")

    active = endpoint_settings.current()
    try:
        settings = endpoint_settings.update(
            payload.get("rotation", active.rotation),
            payload.get("order", list(active.order)),
            payload.get("cooldownSeconds", active.cooldown_seconds),
        )
    except endpoint_settings.InvalidEndpointSettings as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not persist settings: {exc}") from exc
    return {"settings": settings.as_dict()}


@router.post("/api/dashboard/endpoints/test")
async def dashboard_test_endpoints(request: Request) -> dict[str, Any]:
    _require_auth(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    model = payload.get("model") if isinstance(payload, dict) else None
    only = payload.get("only") if isinstance(payload, dict) else None
    try:
        return await endpoint_probe.test_endpoints(request.app.state.account_manager, model=model, only=only)
    except endpoint_probe.ProbeBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except endpoint_probe.ProbeUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/api/dashboard/endpoints/ping")
async def dashboard_ping_endpoints(request: Request) -> dict[str, Any]:
    _require_auth(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    try:
        reps = int(payload.get("reps", endpoint_probe.PING_REPS_DEFAULT))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="reps must be an integer") from None
    try:
        return await endpoint_probe.ping_endpoints(
            request.app.state.account_manager, reps=reps, model=payload.get("model"), only=payload.get("only")
        )
    except endpoint_probe.ProbeBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except endpoint_probe.ProbeUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _probe_region(request: Request) -> str:
    """Best-effort region for display only."""
    try:
        return request.app.state.account_manager.get_first_account().auth_manager.api_region
    except Exception:
        return "us-east-1"


@router.get("/api/dashboard/request-logs/{log_id}")
async def dashboard_request_log_detail(log_id: int, request: Request) -> dict[str, Any]:
    _require_auth(request)
    with _db() as conn:
        row = conn.execute("SELECT * FROM request_logs WHERE id = ?", (log_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown request log")

    keys = row.keys()
    return {
        "id": row["id"],
        "createdAt": row["created_at"],
        "route": row["route"],
        "model": row["model"],
        "statusCode": row["status_code"],
        "latencyMs": row["latency_ms"],
        "clientIp": row["client_ip"] if "client_ip" in keys else None,
        "userAgent": row["user_agent"] if "user_agent" in keys else None,
        "inputTokens": row["input_tokens"] if "input_tokens" in keys else None,
        "outputTokens": row["output_tokens"] if "output_tokens" in keys else None,
        # What Kiro reported for this request, not a figure derived from tokens.
        "creditsSpent": row["credits"] if "credits" in keys else None,
        "modelMultiplier": model_costs.multiplier_for(row["model"]),
    }


@router.get("/api/dashboard/data")
async def dashboard_data_overview(request: Request) -> dict[str, Any]:
    _require_auth(request)
    with _db() as conn:
        logs = conn.execute("SELECT COUNT(*) FROM request_logs").fetchone()[0]
        oldest = conn.execute("SELECT MIN(created_at) FROM request_logs").fetchone()[0]
    path = database_path()
    return {
        "requestLogs": logs,
        "oldestLogAt": oldest,
        "retentionDays": REQUEST_LOG_RETENTION_DAYS,
        "databaseBytes": path.stat().st_size if path.exists() else 0,
    }


@router.post("/api/dashboard/data/clear")
async def dashboard_clear_data(request: Request) -> dict[str, Any]:
    """Delete request logs or the token usage counters."""
    _require_auth(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    scope = (payload or {}).get("scope") if isinstance(payload, dict) else None
    if scope not in ("logs", "usage"):
        raise HTTPException(status_code=400, detail='scope must be "logs" or "usage"')

    with _db() as conn:
        if scope == "logs":
            affected = conn.execute("DELETE FROM request_logs").rowcount
            # The rollups are fed insert-only by a trigger, so clearing the log
            # without them would leave metrics reporting requests that no
            # longer exist anywhere.
            conn.execute("DELETE FROM request_metric_rollups")
            conn.execute("DELETE FROM request_latency_rollups")
        else:
            # Token counters only. Credentials, keys and the request log survive.
            affected = conn.execute("DELETE FROM key_model_usage").rowcount
            affected += conn.execute("DELETE FROM account_model_usage").rowcount
    logger.info(f"[Data] Cleared {scope}: {affected} row(s)")
    return {"scope": scope, "affected": max(0, affected)}


@router.get("/api/dashboard/proxies")
async def dashboard_get_proxies(request: Request) -> dict[str, Any]:
    _require_auth(request)
    return {
        "proxies": proxy_chain.status(),
        "schemes": list(proxy_chain.SUPPORTED_SCHEMES),
        "cooldownSeconds": proxy_chain.COOLDOWN_SECONDS,
    }


@router.put("/api/dashboard/proxies")
async def dashboard_update_proxies(request: Request) -> dict[str, Any]:
    """Replace the proxy chain. An empty list means direct connections."""
    _require_auth(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Expected JSON body") from exc
    if not isinstance(payload, dict) or "proxies" not in payload:
        raise HTTPException(status_code=400, detail='Expected {"proxies": ["socks5://host:1080", ...]}')
    try:
        proxy_chain.set_chain(payload["proxies"])
    except proxy_chain.InvalidProxy as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not persist the chain: {exc}") from exc
    return {"proxies": proxy_chain.status()}


@router.get("/api/dashboard/concurrency")
async def dashboard_concurrency(request: Request) -> dict[str, Any]:
    _require_auth(request)
    snapshot = concurrency.status()
    # Slot keys are internal account identifiers (profile ARNs or credential
    # paths); expose the same opaque digest the accounts view uses instead.
    accounts = snapshot.get("accounts")
    if isinstance(accounts, dict):
        snapshot["accounts"] = {account_label(key): stats for key, stats in accounts.items()}
    return snapshot


@router.get("/api/dashboard/tunables")
async def dashboard_get_tunables(request: Request) -> dict[str, Any]:
    _require_auth(request)
    return gateway_tunables.snapshot()


@router.put("/api/dashboard/tunables")
async def dashboard_update_tunables(request: Request) -> dict[str, Any]:
    _require_auth(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Expected JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")

    try:
        if "tokenRefreshSeconds" in payload:
            gateway_tunables.TOKEN_REFRESH_SECONDS.set(payload["tokenRefreshSeconds"])
        if "loadBalancing" in payload:
            gateway_tunables.LOAD_BALANCING.set(payload["loadBalancing"])
        for field, tunable in (
            ("maxConcurrency", gateway_tunables.MAX_CONCURRENCY),
            ("maxAccountConcurrency", gateway_tunables.MAX_ACCOUNT_CONCURRENCY),
            ("queueTimeoutSeconds", gateway_tunables.QUEUE_TIMEOUT_SECONDS),
        ):
            if field in payload:
                tunable.set(payload[field])
                concurrency.reset()
    except InvalidSetting as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not persist the setting: {exc}") from exc
    return gateway_tunables.snapshot()


@router.get("/api/dashboard/model-costs")
async def dashboard_model_costs(request: Request) -> dict[str, Any]:
    _require_auth(request)
    return {
        "baseline": model_costs.BASELINE_MODEL,
        "models": model_costs.table(),
        "note": (
            "Multipliers are relative to auto at 1.0x. Models sharing a multiplier still "
            "differ in actual consumption: generated tokens, thinking depth and tokenizer "
            "differences all change the credit count."
        ),
    }


@router.post("/api/dashboard/accounts/{label}/enabled")
async def dashboard_set_account_enabled(label: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Expected JSON body") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
        raise HTTPException(status_code=400, detail='Expected {"enabled": true|false}')

    try:
        return await set_account_enabled(request.app.state.account_manager, label, payload["enabled"])
    except AccountNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (DirectoryBackedAccountError, AccountConflictError, LastAccountError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not change the account: {exc}") from exc


@router.get("/api/dashboard/agent-mode")
async def dashboard_get_agent_mode(request: Request) -> dict[str, Any]:
    _require_auth(request)
    return {
        "mode": agent_mode.current(),
        "allowed": list(agent_mode.ALLOWED_MODES),
    }


@router.put("/api/dashboard/agent-mode")
async def dashboard_update_agent_mode(request: Request) -> dict[str, Any]:
    _require_auth(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Expected JSON body") from exc
    if not isinstance(payload, dict) or "mode" not in payload:
        raise HTTPException(status_code=400, detail='Expected {"mode": "vibe"|"spec"|"task"|""}')
    try:
        mode = agent_mode.set_mode(payload["mode"])
    except agent_mode.InvalidAgentMode as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not persist the setting: {exc}") from exc
    return {"mode": mode}


@router.get("/api/dashboard/prompt-filter")
async def dashboard_get_prompt_filter(request: Request) -> dict[str, Any]:
    _require_auth(request)
    return {
        "enabled": prompt_filter.enabled(),
        "identity": prompt_filter.KIRO_IDENTITY,
        "preservedNote": (
            "Only Anthropic's generic sections are dropped. The memory path, environment, "
            "language, skills and anything you supplied are preserved."
        ),
        "droppedSections": sorted(prompt_filter.GENERIC_SECTIONS),
    }


@router.put("/api/dashboard/prompt-filter")
async def dashboard_update_prompt_filter(request: Request) -> dict[str, Any]:
    _require_auth(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Expected JSON body") from exc
    if not isinstance(payload, dict) or "enabled" not in payload:
        raise HTTPException(status_code=400, detail='Expected {"enabled": true|false}')
    try:
        value = prompt_filter.set_enabled(payload["enabled"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not persist the setting: {exc}") from exc
    return {"enabled": value}


@router.get("/api/dashboard/models")
async def dashboard_models(request: Request) -> dict[str, list[dict[str, str]]]:
    _require_auth(request)
    models = request.app.state.account_manager.get_all_available_models() or FALLBACK_MODELS
    return {"models": [{"id": item["modelId"] if isinstance(item, dict) else item} for item in models]}


@router.get("/api/dashboard/request-logs")
async def dashboard_request_logs(
    request: Request,
    limit: int = 25,
    offset: int = 0,
    model: str | None = None,
    order: str = "newest",
) -> dict[str, Any]:
    _require_auth(request)
    limit = max(1, min(limit, 250))
    offset = max(0, offset)
    direction = "ASC" if order == "oldest" else "DESC"

    where = ""
    params: list[Any] = []
    if model:
        # Match the stored name and its normalized form, so filtering by
        # claude-sonnet-4.5 also finds rows logged as claude-sonnet-4-5.
        candidates = {model, normalize_model_name(model) or model}
        where = " WHERE model IN (" + ",".join("?" for _ in candidates) + ")"
        params = list(candidates)

    with _db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM request_logs{where}", params).fetchone()[0]
        rows = conn.execute(
            "SELECT id, created_at, route, model, status_code, latency_ms, client_ip, credits"
            f" FROM request_logs{where} ORDER BY id {direction} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        models = [
            row[0]
            for row in conn.execute("SELECT DISTINCT model FROM request_logs WHERE model IS NOT NULL ORDER BY model")
        ]
    return {
        "logs": [dict(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "hasMore": offset + len(rows) < total,
        "models": models,
        "order": "oldest" if direction == "ASC" else "newest",
    }


@router.get("/metrics", include_in_schema=False)
async def metrics_endpoint(request: Request) -> Response:
    """Prometheus exposition, authenticated with a data-plane API key.

    This is a third plane: not a dashboard cookie session, and not a `/v1` chat
    route. It authenticates with the same bearer key `/v1` uses because that is
    the convention the homelab's other AI gateway already follows, and because a
    scraper cannot hold a cookie. It is still read-only and must never be
    reachable unauthenticated: the exposition carries account quota figures and
    per-key token totals.

    Nothing here is recorded for the scrape's benefit. The numbers come from the
    dashboard's existing SQLite store and the live pool, so the endpoint cannot
    perturb routing or spend upstream quota.
    """
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else ""
    if not token or not verify_data_api_key(token):
        # WWW-Authenticate keeps this a well-formed 401 for the scraper rather
        # than an opaque refusal.
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API Key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    manager = request.app.state.account_manager
    # Flush first: token counters batch in memory, so an unflushed scrape would
    # report totals that lag the traffic by up to one flush interval.
    await asyncio.to_thread(flush_key_model_usage)
    key_names = {ROOT_KEY_ID: "root"}
    try:
        key_names.update({key["id"]: key["name"] for key in list_data_api_keys()})
    except Exception:
        pass

    # The allowlist that bounds the `model` label. Aliases and hidden models are
    # included because they are genuinely served: `auto-kiro` is the alias every
    # Cursor client uses, and leaving it out would file real traffic under
    # `other`.
    models = set(manager.get_all_available_models() or ())
    if not models:
        models = {item["modelId"] if isinstance(item, dict) else item for item in FALLBACK_MODELS}
    models.update(MODEL_ALIASES)
    models.update(HIDDEN_MODELS)
    body = render_metrics(
        started_at=request.app.state.started_at,
        version=APP_VERSION,
        accounts=list(manager._accounts.values()),
        models=models,
        connection_factory=_db,
        usage_for=_cached_usage,
        label_for=account_label,
        state_for=account_routing_state,
        key_names=key_names,
    )
    return Response(content=body, media_type=METRICS_CONTENT_TYPE)


@router.get("/")
async def dashboard_page() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")
