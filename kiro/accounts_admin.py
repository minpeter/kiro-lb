# -*- coding: utf-8 -*-
"""Transactional dashboard-driven Kiro account registration."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from loguru import logger

_ALLOWED_TYPES = {"sqlite", "json", "refresh_token", "internal"}


class AccountNotFoundError(ValueError):
    """The public account label does not identify a registered account."""


class AccountConflictError(ValueError):
    """The account exists but cannot be removed safely."""


class LastAccountError(AccountConflictError):
    """Removing the account would leave no usable account configured."""


class DirectoryBackedAccountError(AccountConflictError):
    """The account comes from a directory-scanning credentials entry."""


def _load_entries(manager: Any) -> list[dict[str, Any]]:
    from kiro.store import load_account_sources

    return load_account_sources()


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
    if entry.get("type") == "internal":
        return str(entry.get("id", ""))
    if entry.get("type") == "refresh_token":
        digest = hashlib.sha256(entry.get("refresh_token", "").encode()).hexdigest()[:16]
        return f"refresh_token_{digest}"
    return str(Path(str(entry.get("path", ""))).expanduser().resolve())


def is_account_deletable(manager: Any, account_id: str) -> bool:
    """Return whether an account is a unique direct entry and not the last account."""
    if len(manager._accounts) <= 1:
        return False

    direct_matches = 0
    for entry in manager._credentials_config:
        source_type = entry.get("type")
        if source_type in {"refresh_token", "internal"}:
            direct_matches += account_id_for_entry(entry) == account_id
            continue
        if source_type not in {"json", "sqlite"}:
            continue

        source_path = Path(str(entry.get("path", ""))).expanduser()
        if source_path.is_dir():
            if Path(account_id).parent == source_path.resolve():
                return False
        elif account_id_for_entry(entry) == account_id:
            direct_matches += 1

    return direct_matches == 1


def build_entry(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a registration request and return an account-source entry."""
    source = str(payload.get("type", "")).strip()
    if source not in _ALLOWED_TYPES:
        raise ValueError(f"type must be one of: {', '.join(sorted(_ALLOWED_TYPES))}")

    entry: dict[str, Any] = {"type": source}
    if source == "internal":
        account_id = str(payload.get("id", "")).strip()
        credential = payload.get("credential")
        if not account_id or not isinstance(credential, dict) or not credential.get("refreshToken"):
            raise ValueError("internal credentials are incomplete")
        entry.update(id=account_id, credential=credential)
    elif source == "refresh_token":
        token = str(payload.get("refreshToken", "")).strip()
        if len(token) < 20:
            raise ValueError("refreshToken is required")
        digest = hashlib.sha256(token.encode()).hexdigest()[:16]
        entry = {
            "type": "internal",
            "id": f"refresh_token_{digest}",
            "credential": {"refreshToken": token},
        }
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


def _snapshot_manager_state(manager: Any) -> dict[str, Any]:
    return {
        "credentials_config": manager._credentials_config,
        "accounts": dict(manager._accounts),
        "model_to_accounts": [
            (model, model_accounts, list(model_accounts.accounts))
            for model, model_accounts in manager._model_to_accounts.items()
        ],
        "current_account_index": manager._current_account_index,
        "rate_observations": manager._rate_observations,
        "unsaved_rate_observations": manager._unsaved_rate_observations,
        "dirty": manager._dirty,
    }


def _restore_manager_state(manager: Any, snapshot: dict[str, Any]) -> None:
    manager._credentials_config = snapshot["credentials_config"]
    manager._accounts.clear()
    manager._accounts.update(snapshot["accounts"])
    manager._model_to_accounts.clear()
    for model, model_accounts, account_ids in snapshot["model_to_accounts"]:
        model_accounts.accounts[:] = account_ids
        manager._model_to_accounts[model] = model_accounts
    manager._current_account_index = snapshot["current_account_index"]
    manager._rate_observations = snapshot["rate_observations"]
    manager._unsaved_rate_observations = snapshot["unsaved_rate_observations"]
    manager._dirty = snapshot["dirty"]


async def remove_account(
    manager: Any,
    label: str,
) -> str:
    """Remove a directly registered account by its public opaque label."""
    from kiro.account_manager import account_label

    async with manager._lock:
        entries = _load_entries(manager)
        direct_entries: dict[str, list[int]] = {}
        directory_paths: list[Path] = []

        for index, entry in enumerate(entries):
            source_type = entry.get("type")
            if source_type in {"refresh_token", "internal"}:
                direct_entries.setdefault(account_id_for_entry(entry), []).append(index)
                continue
            if source_type not in {"json", "sqlite"}:
                continue

            source_path = Path(str(entry.get("path", ""))).expanduser()
            if source_path.is_dir():
                directory_paths.append(source_path.resolve())
            else:
                direct_entries.setdefault(account_id_for_entry(entry), []).append(index)

        candidate_ids = set(manager._accounts) | set(direct_entries)
        matching_ids = [account_id for account_id in candidate_ids if account_label(account_id) == label]
        if (
            len(label) != 12
            or any(character not in "0123456789abcdef" for character in label)
            or len(matching_ids) != 1
        ):
            raise AccountNotFoundError("Unknown account label")

        account_id = matching_ids[0]
        account_path = Path(account_id)
        if any(account_path.parent == directory_path for directory_path in directory_paths):
            raise DirectoryBackedAccountError("Directory-backed accounts cannot be removed individually")

        matching_entries = direct_entries.get(account_id, [])
        if not matching_entries:
            raise AccountNotFoundError("Unknown account label")
        if len(matching_entries) != 1:
            raise AccountConflictError("Account has multiple direct credentials entries")

        entry_index = matching_entries[0]
        remaining_entries = [entry for index, entry in enumerate(entries) if index != entry_index]
        if not remaining_entries or (account_id in manager._accounts and len(manager._accounts) <= 1):
            raise LastAccountError("Cannot remove the last account")

        snapshot = _snapshot_manager_state(manager)
        try:
            from kiro.store import connection, replace_account_sources, save_runtime_state

            manager._credentials_config = remaining_entries
            manager._remove_account_state(account_id)
            state_data = manager._state_document()
            with connection() as conn:
                replace_account_sources(remaining_entries, conn)
                save_runtime_state(state_data, conn, require_write=True)
                conn.execute("DELETE FROM account_usage WHERE account_id = ?", (account_id,))
                conn.execute("DELETE FROM rate_observations WHERE account_id = ?", (account_id,))
        except Exception:
            _restore_manager_state(manager, snapshot)
            raise

        manager._dirty = False
        return account_id


async def register_account(manager: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist a validated credential source and initialize it in the live pool."""
    requested_type = str(payload.get("type", "")).strip()
    entry = build_entry(payload)
    account_id = account_id_for_entry(entry)

    async with manager._lock:
        if account_id in manager._accounts:
            raise ValueError("This credential source is already registered")

        entries = _load_entries(manager)
        snapshot = _snapshot_manager_state(manager)
        entries.append(entry)
        from kiro.account_manager import Account
        from kiro.store import connection, replace_account_sources, save_runtime_state

        try:
            # Mutate the live pool while the proposed source/runtime transaction
            # is uncommitted. A failed ownership check rolls both back together.
            with connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                replace_account_sources(entries, conn)
                manager._credentials_config = entries
                manager._accounts[account_id] = Account(id=account_id)
                save_runtime_state(manager._state_document(), conn, require_write=True)
        except Exception:
            _restore_manager_state(manager, snapshot)
            raise

    # Registration is a control-plane operation, but it must not stall routing
    # while token/model discovery waits on the network.
    initialized = await manager.initialize_account(account_id)
    if not initialized:
        logger.warning("Registered account {} could not be initialized yet", account_id)
    async with manager._lock:
        with connection() as conn:
            save_runtime_state(manager._state_document(), conn, require_write=True)
    return {
        "accountId": hashlib.sha256(account_id.encode()).hexdigest()[:12],
        "accountKey": account_id,
        "type": requested_type,
        "initialized": initialized,
    }
