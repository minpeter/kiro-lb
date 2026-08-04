# -*- coding: utf-8 -*-
"""Dashboard-driven Kiro account registration.

Credential material is written to the account-pool credentials file and is
never returned by the control-plane API. Registration validates the source
before persisting so an unusable entry cannot silently break failover.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Callable

from loguru import logger

_ALLOWED_TYPES = {"sqlite", "json", "refresh_token"}
_RECOVERY_VERSION = 1


class AccountNotFoundError(ValueError):
    """The public account label does not identify a registered account."""


class AccountConflictError(ValueError):
    """The account exists but cannot be removed safely."""


class LastAccountError(AccountConflictError):
    """Removing the account would leave no usable account configured."""


class DirectoryBackedAccountError(AccountConflictError):
    """The account comes from a directory-scanning credentials entry."""


class AccountDeletionRecoveryError(RuntimeError):
    """An account deletion failed and its rollback needs a retry."""


def _credentials_path(manager: Any) -> Path:
    return Path(manager._credentials_file).expanduser()


def _load_entries(manager: Any) -> list[dict[str, Any]]:
    path = _credentials_path(manager)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []
    except Exception as exc:
        raise RuntimeError(f"Existing credentials file is unreadable: {exc}") from exc


def _write_entries(manager: Any, entries: list[dict[str, Any]]) -> None:
    path = _credentials_path(manager)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _recovery_path(manager: Any) -> Path:
    credentials_path = _credentials_path(manager)
    return credentials_path.with_name(f"{credentials_path.name}.account-deletion-recovery")


def _persist_removal_recovery(manager: Any, entries: list[dict[str, Any]], snapshot: dict[str, Any]) -> None:
    path = _recovery_path(manager)
    state_contents = snapshot["state_file"]
    payload = {
        "version": _RECOVERY_VERSION,
        "credentials_entries": entries,
        "state_file": None if state_contents is None else base64.b64encode(state_contents).decode("ascii"),
    }
    descriptor, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        stream = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor = -1
        with stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
    except Exception as exc:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            tmp.unlink(missing_ok=True)
        except OSError as cleanup_error:
            raise RuntimeError(f"Failed to clean up account deletion recovery temp file: {cleanup_error}") from exc
        raise


def _load_removal_recovery(manager: Any) -> tuple[list[dict[str, Any]], bytes | None] | None:
    path = _recovery_path(manager)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != _RECOVERY_VERSION:
            raise ValueError("unsupported recovery record")
        entries = payload.get("credentials_entries")
        if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
            raise ValueError("invalid credentials snapshot")
        encoded_state = payload.get("state_file")
        if encoded_state is not None and not isinstance(encoded_state, str):
            raise ValueError("invalid state snapshot")
        state_contents = None if encoded_state is None else base64.b64decode(encoded_state, validate=True)
    except Exception as exc:
        raise AccountDeletionRecoveryError(f"Account deletion recovery record is unreadable: {exc}") from exc
    return entries, state_contents


def _clear_removal_recovery(manager: Any) -> None:
    _recovery_path(manager).unlink(missing_ok=True)


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


def is_account_deletable(manager: Any, account_id: str) -> bool:
    """Return whether an account is a unique direct entry and not the last account."""
    if len(manager._accounts) <= 1:
        return False

    direct_matches = 0
    for entry in manager._credentials_config:
        source_type = entry.get("type")
        if source_type == "refresh_token":
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
        "state_file": _state_file_snapshot(manager),
    }


def _state_file_snapshot(manager: Any) -> bytes | None:
    state_path = Path(manager._state_file)
    return state_path.read_bytes() if state_path.exists() else None


def _restore_state_contents(manager: Any, state_contents: bytes | None) -> None:
    state_path = Path(manager._state_file)
    if state_contents is None:
        state_path.unlink(missing_ok=True)
    else:
        state_path.write_bytes(state_contents)


def _restore_state_file(manager: Any, snapshot: dict[str, Any]) -> None:
    _restore_state_contents(manager, snapshot["state_file"])


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


async def _rollback_removal(
    manager: Any,
    entries: list[dict[str, Any]],
    snapshot: dict[str, Any],
    *,
    restore_state_file: bool,
) -> None:
    _restore_manager_state(manager, snapshot)
    failures: list[str] = []
    try:
        _write_entries(manager, entries)
    except Exception as exc:
        failures.append(f"credentials.json remains changed ({exc})")
    if restore_state_file:
        try:
            _restore_state_file(manager, snapshot)
        except Exception as exc:
            failures.append(f"state.json remains changed ({exc})")

    if not failures:
        try:
            _clear_removal_recovery(manager)
        except Exception as exc:
            failures.append(f"recovery barrier remains ({exc})")

    if failures:
        try:
            _persist_removal_recovery(manager, entries, snapshot)
        except Exception as exc:
            failures.append(f"recovery barrier could not be created ({exc})")
        manager._account_deletion_recovery = (entries, snapshot, restore_state_file)
        manager._dirty = True
        invariant = "; ".join(failures)
        raise AccountDeletionRecoveryError(
            f"Account deletion rollback is incomplete: live state is restored, but {invariant}. "
            "Retry deletion to run recovery before another mutation."
        )

    manager.__dict__.pop("_account_deletion_recovery", None)
    manager._dirty = snapshot["dirty"]


def recover_pending_account_deletion_files(manager: Any) -> bool:
    """Restore durable rollback snapshots before credentials are loaded or mutated."""
    persisted = _load_removal_recovery(manager)
    if persisted is None:
        return False
    entries, state_contents = persisted
    failures: list[str] = []
    try:
        _write_entries(manager, entries)
    except Exception as exc:
        failures.append(f"credentials.json remains changed ({exc})")
    try:
        _restore_state_contents(manager, state_contents)
    except Exception as exc:
        failures.append(f"state.json remains changed ({exc})")
    if not failures:
        try:
            _clear_removal_recovery(manager)
        except Exception as exc:
            failures.append(f"recovery barrier remains ({exc})")
    if failures:
        manager._dirty = True
        invariant = "; ".join(failures)
        raise AccountDeletionRecoveryError(f"Account deletion recovery is incomplete: {invariant}")
    return True


async def _recover_pending_removal(manager: Any) -> None:
    pending = getattr(manager, "_account_deletion_recovery", None)
    if pending is not None:
        entries, snapshot, restore_state_file = pending
        await _rollback_removal(
            manager,
            entries,
            snapshot,
            restore_state_file=restore_state_file,
        )
        return

    if not recover_pending_account_deletion_files(manager):
        return

    existing = dict(manager._accounts)
    manager._accounts.clear()
    await manager._load_credentials_unlocked()
    for account_id, account in existing.items():
        if account_id in manager._accounts:
            manager._accounts[account_id] = account
    await manager.load_state()


async def remove_account(
    manager: Any,
    label: str,
    *,
    finalize: Callable[[str], None] | None = None,
) -> str:
    """Remove a directly registered account by its public opaque label."""
    from kiro.account_manager import account_label

    async with manager._lock:
        await _recover_pending_removal(manager)
        entries = _load_entries(manager)
        direct_entries: dict[str, list[int]] = {}
        directory_paths: list[Path] = []

        for index, entry in enumerate(entries):
            source_type = entry.get("type")
            if source_type == "refresh_token":
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
            _write_entries(manager, remaining_entries)
            manager._credentials_config = remaining_entries
            manager._remove_account_state(account_id)
            await manager._save_state(raise_errors=True)
            if finalize is not None:
                finalize(account_id)
        except Exception as exc:
            try:
                await _rollback_removal(
                    manager,
                    entries,
                    snapshot,
                    restore_state_file=True,
                )
            except AccountDeletionRecoveryError as recovery_error:
                raise recovery_error from exc
            raise

        manager._dirty = False
        return account_id


async def register_account(manager: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist a validated credential source and initialize it in the live pool."""
    entry = build_entry(payload)
    account_id = account_id_for_entry(entry)

    async with manager._lock:
        await _recover_pending_removal(manager)
        if account_id in manager._accounts:
            raise ValueError("This credential source is already registered")

        entries = _load_entries(manager)
        entries.append(entry)
        _write_entries(manager, entries)

        # Reload so the manager owns the same config it would see on restart, but
        # keep already-initialized accounts so registration never resets the pool.
        existing = dict(manager._accounts)
        await manager._load_credentials_unlocked()
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
