"""Persistent and live account-pool administration."""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import kiro.store as store
from kiro.account_manager import AccountManager, ModelAccountList, account_label
from kiro.accounts_admin import register_account, remove_account
from kiro.auth import KiroAuthManager


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _pool(tmp_path: Path, names: tuple[str, ...]) -> tuple[AccountManager, dict[str, Path], list[dict[str, str]]]:
    sources = {name: tmp_path / f"{name}.json" for name in names}
    for name, source in sources.items():
        _write_json(source, {"refreshToken": f"{name}-token"})
    entries = [{"type": "json", "path": str(sources[name]), "region": "us-west-2"} for name in names]
    store.initialize()
    with store.connection() as conn:
        store.replace_account_sources(entries, conn)
    from kiro.dashboard import initialize_dashboard_store

    initialize_dashboard_store()
    return AccountManager("unused-accounts.json", "unused-runtime.json"), sources, entries


def _snapshot(manager: AccountManager) -> tuple[object, object, tuple[str, ...], bool]:
    return store.load_account_sources(), store.load_runtime_state(), tuple(manager._accounts), manager._dirty


class _ObservedLock:
    def __init__(self, lock: asyncio.Lock, attempted: asyncio.Event):
        self._lock = lock
        self._attempted = attempted

    async def __aenter__(self):
        self._attempted.set()
        await self._lock.acquire()
        return self

    async def __aexit__(self, *args):
        self._lock.release()


@pytest.mark.asyncio
async def test_registration_preserves_initialized_account(tmp_path):
    manager, sources, entries = _pool(tmp_path, ("existing",))
    new_source = tmp_path / "new.json"
    _write_json(new_source, {"refreshToken": "new-token"})
    await manager.load_credentials()
    existing_id = str(sources["existing"].resolve())
    existing = manager._accounts[existing_id]
    existing.auth_manager = KiroAuthManager(creds_file=str(sources["existing"]))
    manager._initialize_account = AsyncMock(return_value=True)

    await register_account(manager, {"type": "json", "path": str(new_source)})

    assert manager._accounts[existing_id] is existing
    assert store.load_account_sources() == [*entries, {"type": "json", "path": str(new_source)}]
    assert store.load_runtime_state() == manager._state_document()


@pytest.mark.asyncio
async def test_removal_updates_store_and_live_pool(tmp_path):
    manager, sources, entries = _pool(tmp_path, ("removed", "survivor"))
    await manager.load_credentials()
    removed_id = str(sources["removed"].resolve())
    survivor_id = str(sources["survivor"].resolve())
    survivor = manager._accounts[survivor_id]
    manager._model_to_accounts = {
        "shared": ModelAccountList(accounts=[removed_id, survivor_id]),
        "removed-only": ModelAccountList(accounts=[removed_id]),
    }

    await remove_account(manager, account_label(removed_id))

    assert store.load_account_sources() == [entries[1]]
    assert tuple(manager._accounts) == (survivor_id,)
    assert manager._accounts[survivor_id] is survivor
    assert manager._model_to_accounts == {"shared": ModelAccountList(accounts=[survivor_id])}
    state = store.load_runtime_state()
    assert state is not None and removed_id not in state["accounts"]
    assert sources["removed"].is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["replace_account_sources", "save_runtime_state"])
async def test_removal_transaction_failure_rolls_back_store_and_manager(tmp_path, monkeypatch, failure_point):
    manager, sources, _ = _pool(tmp_path, ("removed", "survivor"))
    await manager.load_credentials()
    await manager._save_state()
    manager._dirty = False
    removed_id = str(sources["removed"].resolve())
    before = _snapshot(manager)

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError(f"injected {failure_point} failure")

    monkeypatch.setattr(store, failure_point, fail)
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        await remove_account(manager, account_label(removed_id))

    assert _snapshot(manager) == before


@pytest.mark.asyncio
async def test_remove_and_register_serialize_with_manager_lock(tmp_path, monkeypatch):
    manager, sources, entries = _pool(tmp_path, ("removed", "survivor"))
    await manager.load_credentials()
    attempted = asyncio.Event()
    raw_lock = manager._lock
    await raw_lock.acquire()
    monkeypatch.setattr(manager, "_lock", _ObservedLock(raw_lock, attempted))
    task = asyncio.create_task(remove_account(manager, account_label(str(sources["removed"].resolve()))))
    await asyncio.wait_for(attempted.wait(), 1)
    assert not task.done()
    assert store.load_account_sources() == entries
    raw_lock.release()
    await asyncio.wait_for(task, 1)

    new_source = tmp_path / "new.json"
    _write_json(new_source, {"refreshToken": "new-token"})
    manager._initialize_account = AsyncMock(return_value=True)
    await register_account(manager, {"type": "json", "path": str(new_source)})
    assert store.load_account_sources() == [entries[1], {"type": "json", "path": str(new_source)}]


@pytest.mark.asyncio
async def test_removed_account_stays_removed_after_restart(tmp_path):
    manager, sources, entries = _pool(tmp_path, ("removed", "survivor"))
    await manager.load_credentials()
    removed_id = str(sources["removed"].resolve())
    survivor_id = str(sources["survivor"].resolve())
    manager._model_to_accounts = {"shared": ModelAccountList(accounts=[removed_id, survivor_id])}
    await remove_account(manager, account_label(removed_id))

    restarted = AccountManager("unused-accounts.json", "unused-runtime.json")
    await restarted.load_credentials()
    await restarted.load_state()
    restarted._accounts[survivor_id].auth_manager = KiroAuthManager(creds_file=str(sources["survivor"]))

    assert store.load_account_sources() == [entries[1]]
    assert tuple(restarted._accounts) == (survivor_id,)
    assert await restarted.get_next_account("shared") is restarted._accounts[survivor_id]


def test_imports_legacy_recovery_record_once(tmp_path):
    original_entries = [{"type": "json", "path": str(tmp_path / "restored.json")}]
    runtime = {"current_account_index": 0, "accounts": {}}
    recovery = tmp_path / "deletion-recovery.json"
    _write_json(
        recovery,
        {
            "version": 1,
            "credentials_entries": original_entries,
            "state_file": base64.b64encode(json.dumps(runtime).encode()).decode(),
        },
    )

    assert store.import_legacy_files("missing.json", "missing-state.json", str(recovery)) is True
    assert store.load_account_sources() == original_entries
    assert store.load_runtime_state() == runtime
    assert not recovery.exists()
    assert store.import_legacy_files("missing.json", "missing-state.json", str(recovery)) is False


def test_runtime_writer_gate_skips_and_then_writes_atomically(monkeypatch):
    store.initialize()
    monkeypatch.setenv("KIRO_SLOT", "blue")
    store.set_runtime_writer("green")

    assert store.save_runtime_state({"value": "skipped"}) is False
    assert store.load_runtime_state() is None
    with pytest.raises(RuntimeError, match="not the active writer"):
        store.save_runtime_state({"value": "rejected"}, require_write=True)

    store.set_runtime_writer("blue")
    assert store.save_runtime_state({"value": "written"}) is True
    assert store.load_runtime_state() == {"value": "written"}


def test_standby_cannot_mutate_sources_or_internal_credentials(monkeypatch):
    store.initialize()
    entry = {"type": "internal", "id": "account", "credential": {"refreshToken": "original"}}
    with store.connection() as conn:
        store.replace_account_sources([entry], conn)
    monkeypatch.setenv("KIRO_SLOT", "blue")
    store.set_runtime_writer("green")

    with store.connection() as conn, pytest.raises(RuntimeError, match="not the active writer"):
        store.replace_account_sources([], conn)
    with pytest.raises(RuntimeError, match="not the active writer"):
        store.save_internal_credential("account", {"refreshToken": "standby"})

    assert store.load_account_sources() == [{"type": "internal", "id": "account"}]
    assert store.load_internal_credential("account") == {"refreshToken": "original"}


def test_refresh_lease_is_exclusive_across_connections_and_owner_safe():
    store.initialize()
    first = store.try_acquire_refresh_lease("account")
    assert first is not None
    assert store.try_acquire_refresh_lease("account") is None

    store.release_refresh_lease("account", "different-owner")
    assert store.try_acquire_refresh_lease("account") is None
    store.release_refresh_lease("account", first)
    assert store.try_acquire_refresh_lease("account") is not None


def test_legacy_runtime_import_is_not_writer_gated(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_SLOT", "blue")
    store.initialize()
    store.set_runtime_writer("green")
    state_file = tmp_path / "state.json"
    _write_json(state_file, {"current_account_index": 3})

    assert store.import_legacy_files("missing.json", str(state_file)) is True
    assert store.load_runtime_state() == {"current_account_index": 3}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "names,label,error", [(("one", "two"), "000000000000", "unknown"), (("only",), None, "last account")]
)
async def test_rejected_removal_does_not_mutate(tmp_path, names, label, error):
    manager, sources, _ = _pool(tmp_path, names)
    await manager.load_credentials()
    before = _snapshot(manager)
    target = label or account_label(str(sources["only"].resolve()))
    with pytest.raises(ValueError, match=f"(?i){error}"):
        await remove_account(manager, target)
    assert _snapshot(manager) == before
