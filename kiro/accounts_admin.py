# -*- coding: utf-8 -*-
"""Dashboard-driven Kiro account registration.

Credential material is written to the account-pool credentials file and is
never returned by the control-plane API. Registration validates the source
before persisting so an unusable entry cannot silently break failover.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from loguru import logger

_ALLOWED_TYPES = {"sqlite", "json", "refresh_token"}


def _credentials_path() -> Path:
    return Path(os.getenv("ACCOUNTS_CONFIG_FILE", "credentials.json")).expanduser()


def _load_entries() -> list[dict[str, Any]]:
    path = _credentials_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []
    except Exception as exc:
        raise RuntimeError(f"Existing credentials file is unreadable: {exc}") from exc


def _write_entries(entries: list[dict[str, Any]]) -> None:
    path = _credentials_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _validate_sqlite(path: Path) -> None:
    if not path.is_file():
        raise ValueError("SQLite credential file does not exist in the server filesystem")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            found = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='auth_kv'").fetchone()
    except Exception as exc:
        raise ValueError(f"Cannot read SQLite credential database: {exc}") from exc
    if not found:
        raise ValueError("SQLite database has no auth_kv table; not a Kiro CLI credential store")


def _validate_json(path: Path) -> None:
    if not path.is_file():
        raise ValueError("JSON credential file does not exist in the server filesystem")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Cannot parse JSON credential file: {exc}") from exc
    if not isinstance(data, dict) or not ({"refreshToken", "clientId"} & set(data)):
        raise ValueError("JSON credential file has neither refreshToken nor clientId")


def account_id_for_entry(entry: dict[str, Any]) -> str:
    if entry.get("type") == "refresh_token":
        digest = hashlib.sha256(entry.get("refresh_token", "").encode()).hexdigest()[:16]
        return f"refresh_token_{digest}"
    return str(Path(str(entry.get("path", ""))).expanduser().resolve())


def build_entry(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a registration request and return a credentials-file entry."""
    source = str(payload.get("type", "")).strip()
    if source not in _ALLOWED_TYPES:
        raise ValueError(f"type must be one of: {', '.join(sorted(_ALLOWED_TYPES))}")

    entry: dict[str, Any] = {"type": source}
    if source == "refresh_token":
        token = str(payload.get("refreshToken", "")).strip()
        if len(token) < 20:
            raise ValueError("refreshToken is required")
        entry["refresh_token"] = token
    else:
        raw_path = str(payload.get("path", "")).strip()
        if not raw_path:
            raise ValueError("path is required")
        path = Path(raw_path).expanduser()
        if source == "sqlite":
            _validate_sqlite(path)
        else:
            _validate_json(path)
        entry["path"] = str(path)

    for field, key in (("profileArn", "profile_arn"), ("region", "region"), ("apiRegion", "api_region")):
        value = str(payload.get(field, "")).strip()
        if value:
            entry[key] = value
    return entry


async def register_account(manager: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist a validated credential source and initialize it in the live pool."""
    entry = build_entry(payload)
    account_id = account_id_for_entry(entry)
    if account_id in manager._accounts:
        raise ValueError("This credential source is already registered")

    entries = _load_entries()
    entries.append(entry)
    _write_entries(entries)

    # Reload so the manager owns the same config it would see on restart, but
    # keep already-initialized accounts so registration never resets the pool.
    existing = dict(manager._accounts)
    await manager.load_credentials()
    for known_id, known_account in existing.items():
        if known_id in manager._accounts:
            manager._accounts[known_id] = known_account
    if account_id not in manager._accounts:
        raise RuntimeError("Credential source was saved but produced no usable account")

    initialized = await manager._initialize_account(account_id)
    if not initialized:
        logger.warning("Registered account {} could not be initialized yet", account_id)
    await manager._save_state()
    return {
        "accountId": hashlib.sha256(account_id.encode()).hexdigest()[:12],
        "accountKey": account_id,
        "type": entry["type"],
        "initialized": initialized,
    }
