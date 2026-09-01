# -*- coding: utf-8 -*-
"""Shared private SQLite persistence for gateway and dashboard state."""

from __future__ import annotations

import base64
import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from loguru import logger

DB_FILENAME = "dashboard.sqlite3"
LEGACY_MIGRATION = "gateway-json-v1"


def database_path() -> Path:
    return Path(os.getenv("DASHBOARD_DATA_DIR", "data")) / DB_FILENAME


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    os.chmod(path, 0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def initialize() -> None:
    with connection() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS account_sources (
                account_id TEXT PRIMARY KEY,
                position INTEGER NOT NULL,
                config_json TEXT NOT NULL,
                credential_json TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS account_runtime (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                state_json TEXT NOT NULL
            )"""
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS store_migrations (name TEXT PRIMARY KEY, completed_at INTEGER NOT NULL)"
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS runtime_writer (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                slot TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS credential_refresh_leases (
                account_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                expires_at REAL NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            )"""
        )


def load_setting(key: str) -> Any | None:
    """Return a persisted setting, or None when absent or unreadable.

    Never raises: a corrupt row must fall back to the environment default
    rather than block startup.
    """
    try:
        initialize()
        with connection() as conn:
            row = conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
    except Exception as exc:
        logger.warning(f"[Store] Could not read setting {key!r}: {exc}")
        return None
    if not row:
        return None
    try:
        return json.loads(row["value_json"])
    except (TypeError, ValueError) as exc:
        logger.warning(f"[Store] Discarding malformed setting {key!r}: {exc}")
        return None


def save_setting(key: str, value: Any) -> None:
    """Persist a setting. Raises so the caller can report the failure."""
    initialize()
    payload = json.dumps(value)
    with connection() as conn:
        conn.execute(
            "INSERT INTO settings(key, value_json) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
            (key, payload),
        )


def set_runtime_writer(slot: str) -> None:
    """Assign runtime-state persistence to the active blue/green slot."""
    initialize()
    with connection() as conn:
        conn.execute(
            "INSERT INTO runtime_writer(id, slot) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET slot=excluded.slot",
            (slot,),
        )


def can_write_runtime_state() -> bool:
    """Return whether this process owns runtime-state writes.

    Non-slot deployments remain single-writer and need no deploy coordination.
    """
    slot = os.getenv("KIRO_SLOT", "")
    if not slot:
        return True
    with connection() as conn:
        row = conn.execute("SELECT slot FROM runtime_writer WHERE id = 1").fetchone()
    return bool(row and row[0] == slot)


def require_runtime_writer(conn: sqlite3.Connection) -> None:
    """Reject gateway-owned mutations from an inactive blue/green slot."""
    slot = os.getenv("KIRO_SLOT", "")
    if not slot:
        return
    row = conn.execute("SELECT slot FROM runtime_writer WHERE id = 1").fetchone()
    if not row or row[0] != slot:
        raise RuntimeError(f"gateway store write rejected: slot {slot!r} is not the active writer")


def load_account_sources(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    if conn is None:
        with connection() as owned:
            return load_account_sources(owned)
    return [json.loads(row[0]) for row in conn.execute("SELECT config_json FROM account_sources ORDER BY position")]


def replace_account_sources(entries: list[dict[str, Any]], conn: sqlite3.Connection, *, ungated: bool = False) -> None:
    if not ungated:
        require_runtime_writer(conn)
    existing_credentials = {
        row["account_id"]: row["credential_json"]
        for row in conn.execute("SELECT account_id, credential_json FROM account_sources")
    }
    conn.execute("DELETE FROM account_sources")
    for position, entry in enumerate(entries):
        account_id = account_id_for_entry(entry)
        credential = entry.get("credential") if entry.get("type") == "internal" else None
        credential_json = existing_credentials.get(account_id)
        if credential_json is None and credential is not None:
            credential_json = json.dumps(credential)
        stored = {key: value for key, value in entry.items() if key != "credential"}
        conn.execute(
            "INSERT INTO account_sources(account_id, position, config_json, credential_json) VALUES (?, ?, ?, ?)",
            (account_id, position, json.dumps(stored), credential_json),
        )


def account_id_for_entry(entry: dict[str, Any]) -> str:
    if entry.get("type") == "internal":
        return str(entry["id"])
    if entry.get("type") == "refresh_token":
        import hashlib

        digest = hashlib.sha256(str(entry.get("refresh_token", "")).encode()).hexdigest()[:16]
        return f"refresh_token_{digest}"
    return str(Path(str(entry.get("path", ""))).expanduser().resolve())


def load_internal_credential(account_id: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT credential_json FROM account_sources WHERE account_id = ?", (account_id,)).fetchone()
    return json.loads(row[0]) if row and row[0] else None


def save_internal_credential(account_id: str, document: dict[str, Any]) -> None:
    with connection() as conn:
        require_runtime_writer(conn)
        updated = conn.execute(
            "UPDATE account_sources SET credential_json = ? WHERE account_id = ? AND credential_json IS NOT NULL",
            (json.dumps(document), account_id),
        ).rowcount
        if not updated:
            raise KeyError(f"Unknown internal account: {account_id}")


def refresh_internal_credential(account_id: str) -> dict[str, Any]:
    """Load the latest internal credential immediately before a refresh."""
    document = load_internal_credential(account_id)
    if document is None:
        raise KeyError(f"Unknown internal account: {account_id}")
    return document


def try_acquire_refresh_lease(account_id: str, lease_seconds: float = 60.0) -> str | None:
    """Atomically acquire a short per-account cross-process refresh lease."""
    initialize()
    owner = uuid.uuid4().hex
    now = time.time()
    with connection() as conn:
        slot = os.getenv("KIRO_SLOT", "")
        rowcount = conn.execute(
            """INSERT INTO credential_refresh_leases(account_id, owner, expires_at)
               SELECT ?, ?, ?
               WHERE ? = '' OR EXISTS (
                   SELECT 1 FROM runtime_writer WHERE id = 1 AND slot = ?
               )
               ON CONFLICT(account_id) DO UPDATE SET owner=excluded.owner, expires_at=excluded.expires_at
               WHERE credential_refresh_leases.expires_at <= ?""",
            (account_id, owner, now + lease_seconds, slot, slot, now),
        ).rowcount
    return owner if rowcount else None


def release_refresh_lease(account_id: str, owner: str) -> None:
    with connection() as conn:
        conn.execute(
            "DELETE FROM credential_refresh_leases WHERE account_id = ? AND owner = ?",
            (account_id, owner),
        )


def load_runtime_state() -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT state_json FROM account_runtime WHERE id = 1").fetchone()
    return json.loads(row[0]) if row else None


def load_quota_headroom() -> dict[str, float]:
    """Return the last known unused-quota fraction per account.

    Quota-weighted routing needs a weight from the first request onward, but the
    live reading comes from a poll that runs after startup and again only every
    USAGE_REFRESH_INTERVAL_SECONDS. Seeding from the persisted usage rows closes
    that window for both a cold start and a blue/green handoff, where
    ``reload_durable_state`` rebuilds the pool from scratch.

    Rows carrying an error, a non-numeric reading, or a non-positive limit are
    omitted so the router sees "unknown" instead of a fabricated ratio. The
    ``account_usage`` table belongs to the dashboard and may not exist yet on a
    fresh database, which is not an error here.
    """
    try:
        with connection() as conn:
            rows = conn.execute(
                """SELECT account_id, current_usage, usage_limit FROM account_usage
                   WHERE error IS NULL AND current_usage IS NOT NULL AND usage_limit > 0"""
            ).fetchall()
    except sqlite3.Error:
        return {}

    headroom: dict[str, float] = {}
    for row in rows:
        try:
            current = float(row["current_usage"])
            limit = float(row["usage_limit"])
        except (TypeError, ValueError):
            continue
        if limit <= 0:
            continue
        headroom[row["account_id"]] = max(0.0, min(1.0, 1.0 - (current / limit)))
    return headroom


def load_quota_period() -> dict[str, tuple[float | None, bool | None]]:
    """Return each account's next quota reset and overage flag.

    A 402 quarantine should end when the monthly allowance actually resets, not
    after a fixed interval, and a spent account should be reported as spent. Both
    facts are known only to the control-plane usage poll, so a process that
    restarts mid-quarantine has to recover them from the persisted rows: without
    the reset date it falls back to the fixed window and re-admits an account
    whose quota is still gone.

    ``next_date_reset`` is TEXT and has held both epoch seconds and an empty
    string, so anything unparseable or non-positive becomes None instead of a
    guess. Only the two conclusive overage values map to a bool. The table
    belongs to the dashboard and may not exist yet on a fresh database.

    Errored rows are excluded, matching ``load_quota_headroom``. A failed poll
    rewrites only ``updated_at`` and ``error``, leaving the previous reset date
    and overage flag in place, so reading those rows would let a restart revive
    facts the current poll could not confirm - and quarantine an account until an
    obsolete reset instead of falling back to the fixed window.
    """
    try:
        with connection() as conn:
            rows = conn.execute(
                "SELECT account_id, next_date_reset, overage_status FROM account_usage WHERE error IS NULL"
            ).fetchall()
    except sqlite3.Error:
        return {}

    period: dict[str, tuple[float | None, bool | None]] = {}
    for row in rows:
        try:
            reset_at: float | None = float(row["next_date_reset"])
        except (TypeError, ValueError):
            reset_at = None
        # A stored "inf"/"nan" parses but is not a date, and would break JSON
        # serialization once the seeded value is echoed back on the accounts route.
        if reset_at is not None and (not math.isfinite(reset_at) or reset_at <= 0):
            reset_at = None

        status = row["overage_status"]
        overage: bool | None = None
        if isinstance(status, str):
            normalized = status.strip().upper()
            if normalized == "ENABLED":
                overage = True
            elif normalized == "DISABLED":
                overage = False

        if reset_at is None and overage is None:
            continue
        period[row["account_id"]] = (reset_at, overage)
    return period


def save_runtime_state(
    state: dict[str, Any],
    conn: sqlite3.Connection | None = None,
    *,
    ungated: bool = False,
    require_write: bool = False,
) -> bool:
    """Persist runtime state if this process is the current writer.

    The ownership predicate is part of the UPSERT, so a deploy handoff cannot
    race a check performed before the write transaction. Migration uses
    ``ungated`` because imported state predates blue/green ownership.
    """
    if conn is None:
        with connection() as owned:
            return save_runtime_state(state, owned, ungated=ungated, require_write=require_write)
    slot = os.getenv("KIRO_SLOT", "")
    written = bool(
        conn.execute(
            """INSERT INTO account_runtime(id, state_json)
               SELECT 1, ?
               WHERE ? OR ? = '' OR EXISTS (
                   SELECT 1 FROM runtime_writer WHERE id = 1 AND slot = ?
               )
               ON CONFLICT(id) DO UPDATE SET state_json=excluded.state_json""",
            (json.dumps(state), ungated, slot, slot),
        ).rowcount
    )
    if require_write and not written:
        raise RuntimeError(f"runtime state write rejected: slot {slot!r} is not the active writer")
    return written


def _read_json(path: Path, expected: type[Any]) -> Any | None:
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, expected):
        raise ValueError(f"{path} has the wrong JSON shape")
    return value


def canonicalize_account_sources(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    canonical: list[dict[str, Any]] = []
    for original in entries:
        entry = dict(original)
        if entry.get("type") == "refresh_token":
            token = str(entry.pop("refresh_token", ""))
            if not token:
                raise ValueError("legacy refresh_token source has no token")
            account_id = account_id_for_entry(original)
            entry.update(type="internal", id=account_id, credential={"refreshToken": token})
        canonical.append(entry)
    return canonical


def import_legacy_files(credentials_file: str, state_file: str, recovery_file: str | None = None) -> bool:
    """Import gateway JSON once without replacing an already populated store."""
    initialize()
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM store_migrations WHERE name=?", (LEGACY_MIGRATION,)).fetchone():
            return False
        entries: list[dict[str, Any]] | None = None
        state: dict[str, Any] | None = None
        recovery_path = Path(recovery_file).expanduser() if recovery_file else None
        if recovery_path and recovery_path.exists():
            payload = _read_json(recovery_path, dict)
            assert isinstance(payload, dict)
            if payload.get("version") != 1 or not isinstance(payload.get("credentials_entries"), list):
                raise ValueError("invalid account deletion recovery record")
            entries = payload["credentials_entries"]
            encoded = payload.get("state_file")
            if encoded is not None:
                state = json.loads(base64.b64decode(encoded, validate=True))
                if not isinstance(state, dict):
                    raise ValueError("invalid state in account deletion recovery record")
        else:
            entries = _read_json(Path(credentials_file).expanduser(), list)
            state = _read_json(Path(state_file).expanduser(), dict)
        if entries is not None and not all(isinstance(item, dict) for item in entries):
            raise ValueError("legacy credentials contain a non-object entry")
        if not conn.execute("SELECT 1 FROM account_sources LIMIT 1").fetchone() and entries:
            replace_account_sources(canonicalize_account_sources(entries), conn, ungated=True)
        if not conn.execute("SELECT 1 FROM account_runtime WHERE id=1").fetchone() and state is not None:
            save_runtime_state(state, conn, ungated=True, require_write=True)
        conn.execute(
            "INSERT INTO store_migrations(name, completed_at) VALUES (?, unixepoch())",
            (LEGACY_MIGRATION,),
        )
    if recovery_path and recovery_path.exists():
        recovery_path.unlink()
    return True
