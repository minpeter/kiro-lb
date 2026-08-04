"""Persistent and live account-pool administration."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

import kiro.accounts_admin as accounts_admin_module
from kiro.account_manager import AccountManager, AccountStats, ModelAccountList, RateObservation, account_label
from kiro.accounts_admin import AccountDeletionRecoveryError, register_account, remove_account
from kiro.auth import KiroAuthManager


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _direct_pool(
    tmp_path: Path, monkeypatch, names: tuple[str, ...]
) -> tuple[AccountManager, Path, Path, dict[str, Path], list[dict[str, str]]]:
    credentials_file = tmp_path / "credentials.json"
    state_file = tmp_path / "state.json"
    sources = {name: tmp_path / f"{name}.json" for name in names}
    for name, source in sources.items():
        _write_json(source, {"refreshToken": f"{name}-token"})
    entries = [{"type": "json", "path": str(sources[name]), "region": "us-west-2"} for name in names]
    _write_json(credentials_file, entries)
    monkeypatch.setenv("ACCOUNTS_CONFIG_FILE", str(credentials_file))
    return AccountManager(str(credentials_file), str(state_file)), credentials_file, state_file, sources, entries


def _parsed(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _observation_state(observation: RateObservation) -> tuple[float, str, int, bool, str]:
    return (
        observation.at,
        observation.account_id,
        observation.rpm,
        observation.rejected,
        observation.outcome,
    )


def _manager_state(manager: AccountManager) -> dict[str, object]:
    return {
        "dirty": manager._dirty,
        "accounts": {
            account_id: (
                id(account),
                id(account.auth_manager),
                account.failures,
                account.last_failure_time,
                account.rate_limited_until,
                account.quota_exhausted_until,
                account.suspended_until,
                account.models_cached_at,
                (
                    account.stats.total_requests,
                    account.stats.successful_requests,
                    account.stats.failed_requests,
                ),
            )
            for account_id, account in manager._accounts.items()
        },
        "credentials_config": json.loads(json.dumps(manager._credentials_config)),
        "model_to_accounts": {
            model: list(model_accounts.accounts) for model, model_accounts in manager._model_to_accounts.items()
        },
        "current_account_index": manager._current_account_index,
        "rate_observations": [_observation_state(item) for item in manager._rate_observations],
        "unsaved_rate_observations": [_observation_state(item) for item in manager._unsaved_rate_observations],
    }


def _disk_and_manager_state(
    manager: AccountManager, credentials_file: Path, state_file: Path
) -> tuple[object, object, dict[str, object]]:
    return _parsed(credentials_file), _parsed(state_file), _manager_state(manager)


class _ObservedLock:
    def __init__(self, lock: asyncio.Lock, attempted: asyncio.Event):
        self._lock = lock
        self._attempted = attempted

    async def acquire(self) -> bool:
        self._attempted.set()
        return await self._lock.acquire()

    def release(self) -> None:
        self._lock.release()

    async def __aenter__(self) -> _ObservedLock:
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


@pytest.mark.asyncio
async def test_direct_registration_preserves_initialized_account_and_credentials_entry(tmp_path, monkeypatch):
    credentials_file = tmp_path / "credentials.json"
    state_file = tmp_path / "state.json"
    existing_source = tmp_path / "existing.json"
    new_source = tmp_path / "new.json"
    _write_json(existing_source, {"refreshToken": "existing-token"})
    _write_json(new_source, {"refreshToken": "new-token"})
    existing_entry = {"type": "json", "path": str(existing_source), "region": "us-west-2"}
    new_entry = {"type": "json", "path": str(new_source)}
    _write_json(credentials_file, [existing_entry])
    monkeypatch.setenv("ACCOUNTS_CONFIG_FILE", str(credentials_file))

    manager = AccountManager(str(credentials_file), str(state_file))
    await manager.load_credentials()
    existing_id = str(existing_source.resolve())
    initialized_account = manager._accounts[existing_id]
    initialized_account.auth_manager = KiroAuthManager(creds_file=str(existing_source))
    manager._initialize_account = AsyncMock(return_value=True)

    await register_account(manager, {"type": "json", "path": str(new_source)})

    assert manager._accounts[existing_id] is initialized_account
    assert manager._credentials_config == [existing_entry, new_entry]
    assert _parsed(credentials_file) == [existing_entry, new_entry]


@pytest.mark.asyncio
async def test_remove_registered_account_updates_config_runtime_state(tmp_path, monkeypatch):
    manager, credentials_file, state_file, sources, entries = _direct_pool(
        tmp_path, monkeypatch, ("removed", "survivor")
    )
    await manager.load_credentials()
    removed_id = str(sources["removed"].resolve())
    survivor_id = str(sources["survivor"].resolve())
    removed_account = manager._accounts[removed_id]
    survivor_account = manager._accounts[survivor_id]
    removed_account.failures = 7
    survivor_account.failures = 2
    survivor_account.last_failure_time = 125.0
    survivor_account.stats = AccountStats(total_requests=8, successful_requests=6, failed_requests=2)
    manager._model_to_accounts = {
        "shared-model": ModelAccountList(accounts=[removed_id, survivor_id]),
        "removed-only-model": ModelAccountList(accounts=[removed_id]),
        "survivor-only-model": ModelAccountList(accounts=[survivor_id]),
    }
    manager._current_account_index = 1
    removed_observation = RateObservation(10.0, removed_id, 3, True, "rate_limited")
    survivor_observation = RateObservation(20.0, survivor_id, 4, False, "success")
    manager._rate_observations = [removed_observation, survivor_observation]
    manager._unsaved_rate_observations = [removed_observation, survivor_observation]
    await manager._save_state()

    await remove_account(manager, account_label(removed_id))

    assert _parsed(credentials_file) == [entries[1]]
    assert manager._credentials_config == [entries[1]]
    assert list(manager._accounts) == [survivor_id]
    assert manager._accounts[survivor_id] is survivor_account
    assert manager._current_account_index == 0
    assert manager._model_to_accounts == {
        "shared-model": ModelAccountList(accounts=[survivor_id]),
        "survivor-only-model": ModelAccountList(accounts=[survivor_id]),
    }
    assert [_observation_state(item) for item in manager._rate_observations] == [
        _observation_state(survivor_observation)
    ]
    assert [_observation_state(item) for item in manager._unsaved_rate_observations] == [
        _observation_state(survivor_observation)
    ]
    assert _parsed(state_file) == {
        "current_account_index": 0,
        "accounts": {
            survivor_id: {
                "failures": 2,
                "last_failure_time": 125.0,
                "quota_exhausted_until": 0.0,
                "suspended_until": 0.0,
                "models_cached_at": 0.0,
                "stats": {"total_requests": 8, "successful_requests": 6, "failed_requests": 2},
            }
        },
        "model_to_accounts": {
            "shared-model": {"accounts": [survivor_id]},
            "survivor-only-model": {"accounts": [survivor_id]},
        },
    }
    assert sources["removed"].is_file()
    assert _parsed(sources["removed"]) == {"refreshToken": "removed-token"}


@pytest.mark.asyncio
async def test_remove_account_rolls_back_when_required_state_save_fails(tmp_path, monkeypatch):
    manager, credentials_file, state_file, sources, entries = _direct_pool(
        tmp_path, monkeypatch, ("removed", "survivor")
    )
    await manager.load_credentials()
    removed_id = str(sources["removed"].resolve())
    survivor_id = str(sources["survivor"].resolve())
    removed_account = manager._accounts[removed_id]
    survivor_account = manager._accounts[survivor_id]
    manager._model_to_accounts = {
        "shared-model": ModelAccountList(accounts=[removed_id, survivor_id]),
        "removed-only-model": ModelAccountList(accounts=[removed_id]),
    }
    manager._current_account_index = 1
    removed_observation = RateObservation(10.0, removed_id, 3, True, "rate_limited")
    survivor_observation = RateObservation(20.0, survivor_id, 4, False, "success")
    manager._rate_observations = [removed_observation, survivor_observation]
    manager._unsaved_rate_observations = [removed_observation, survivor_observation]
    await manager._save_state()
    manager._dirty = False
    before = _disk_and_manager_state(manager, credentials_file, state_file)

    original_replace = Path.replace
    state_tmp = state_file.with_suffix(".json.tmp")

    def fail_state_replace(path: Path, target: Path) -> Path:
        if path == state_tmp:
            raise OSError("injected state persistence failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_state_replace)

    with pytest.raises(OSError, match="injected state persistence failure"):
        await remove_account(manager, account_label(removed_id))

    assert _disk_and_manager_state(manager, credentials_file, state_file) == before
    assert manager._accounts[removed_id] is removed_account
    assert manager._accounts[survivor_id] is survivor_account
    assert sources["removed"].is_file()

    monkeypatch.setattr(Path, "replace", original_replace)
    await remove_account(manager, account_label(removed_id))

    assert _parsed(credentials_file) == [entries[1]]
    assert tuple(manager._accounts) == (survivor_id,)
    persisted_state = _parsed(state_file)
    assert isinstance(persisted_state, dict)
    assert removed_id not in persisted_state["accounts"]


@pytest.mark.asyncio
async def test_remove_account_serializes_with_manager_lock(tmp_path, monkeypatch):
    manager, credentials_file, _, sources, entries = _direct_pool(tmp_path, monkeypatch, ("removed", "survivor"))
    await manager.load_credentials()
    removed_id = str(sources["removed"].resolve())
    attempted = asyncio.Event()
    raw_lock = manager._lock
    await raw_lock.acquire()
    monkeypatch.setattr(manager, "_lock", _ObservedLock(raw_lock, attempted))
    delete_task: asyncio.Task[str] | None = None
    attempted_task: asyncio.Task[bool] | None = None

    try:
        delete_task = asyncio.create_task(remove_account(manager, account_label(removed_id)))
        attempted_task = asyncio.create_task(attempted.wait())
        done, _ = await asyncio.wait_for(
            asyncio.wait({delete_task, attempted_task}, return_when=asyncio.FIRST_COMPLETED),
            timeout=1,
        )

        assert attempted_task in done, "remove_account did not serialize through AccountManager._lock"
        assert delete_task not in done
        assert _parsed(credentials_file) == entries
        assert list(manager._accounts) == [str(source.resolve()) for source in sources.values()]

        raw_lock.release()
        await asyncio.wait_for(delete_task, timeout=1)
    finally:
        if raw_lock.locked():
            raw_lock.release()
        for task in (delete_task, attempted_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (delete_task, attempted_task) if task is not None), return_exceptions=True
        )

    assert _parsed(credentials_file) == [entries[1]]
    assert list(manager._accounts) == [str(sources["survivor"].resolve())]


@pytest.mark.asyncio
async def test_remove_account_rejects_unknown_label_without_changes(tmp_path, monkeypatch):
    manager, credentials_file, state_file, _, _ = _direct_pool(tmp_path, monkeypatch, ("first", "second"))
    await manager.load_credentials()
    await manager._save_state()
    before = _disk_and_manager_state(manager, credentials_file, state_file)

    with pytest.raises(ValueError, match="(?i)unknown account"):
        await remove_account(manager, "000000000000")

    assert _disk_and_manager_state(manager, credentials_file, state_file) == before


@pytest.mark.asyncio
async def test_remove_account_rejects_last_account_without_changes(tmp_path, monkeypatch):
    manager, credentials_file, state_file, sources, _ = _direct_pool(tmp_path, monkeypatch, ("only",))
    await manager.load_credentials()
    await manager._save_state()
    before = _disk_and_manager_state(manager, credentials_file, state_file)
    only_id = str(sources["only"].resolve())

    with pytest.raises(ValueError, match="(?i)last account"):
        await remove_account(manager, account_label(only_id))

    assert _disk_and_manager_state(manager, credentials_file, state_file) == before
    assert sources["only"].is_file()


@pytest.mark.asyncio
async def test_remove_account_rejects_directory_backed_account_without_changes(tmp_path, monkeypatch):
    credentials_file = tmp_path / "credentials.json"
    state_file = tmp_path / "state.json"
    credentials_directory = tmp_path / "scanned"
    credentials_directory.mkdir()
    scanned_source = credentials_directory / "scanned.json"
    direct_source = tmp_path / "direct.json"
    _write_json(scanned_source, {"refreshToken": "scanned-token"})
    _write_json(direct_source, {"refreshToken": "direct-token"})
    entries = [
        {"type": "json", "path": str(credentials_directory)},
        {"type": "json", "path": str(direct_source)},
    ]
    _write_json(credentials_file, entries)
    monkeypatch.setenv("ACCOUNTS_CONFIG_FILE", str(credentials_file))
    manager = AccountManager(str(credentials_file), str(state_file))
    await manager.load_credentials()
    await manager._save_state()
    before = _disk_and_manager_state(manager, credentials_file, state_file)

    with pytest.raises(ValueError, match="(?i)directory"):
        await remove_account(manager, account_label(str(scanned_source.resolve())))

    assert _disk_and_manager_state(manager, credentials_file, state_file) == before
    assert scanned_source.is_file()
    assert direct_source.is_file()


@pytest.mark.asyncio
async def test_remove_account_repeated_delete_is_unknown_and_preserves_survivor(tmp_path, monkeypatch):
    manager, credentials_file, state_file, sources, entries = _direct_pool(
        tmp_path, monkeypatch, ("removed", "survivor")
    )
    await manager.load_credentials()
    removed_id = str(sources["removed"].resolve())
    removed_label = account_label(removed_id)
    await remove_account(manager, removed_label)
    after_first_delete = _disk_and_manager_state(manager, credentials_file, state_file)

    with pytest.raises(ValueError, match="(?i)unknown account"):
        await remove_account(manager, removed_label)

    assert _disk_and_manager_state(manager, credentials_file, state_file) == after_first_delete
    assert _parsed(credentials_file) == [entries[1]]
    assert list(manager._accounts) == [str(sources["survivor"].resolve())]
    assert sources["removed"].is_file()


@pytest.mark.asyncio
async def test_removed_account_does_not_return_after_restart_and_survivor_routes(tmp_path, monkeypatch):
    manager, credentials_file, state_file, sources, entries = _direct_pool(
        tmp_path, monkeypatch, ("removed", "survivor")
    )
    await manager.load_credentials()
    removed_id = str(sources["removed"].resolve())
    survivor_id = str(sources["survivor"].resolve())
    manager._model_to_accounts = {
        "shared-model": ModelAccountList(accounts=[removed_id, survivor_id]),
        "removed-only-model": ModelAccountList(accounts=[removed_id]),
    }
    manager._current_account_index = 1

    await remove_account(manager, account_label(removed_id))

    restarted = AccountManager(str(credentials_file), str(state_file))
    await restarted.load_credentials()
    await restarted.load_state()
    restarted._accounts[survivor_id].auth_manager = KiroAuthManager(creds_file=str(sources["survivor"]))
    routed = await restarted.get_next_account("shared-model")

    assert _parsed(credentials_file) == [entries[1]]
    assert list(restarted._accounts) == [survivor_id]
    assert restarted._current_account_index == 0
    assert all(removed_id not in model_accounts.accounts for model_accounts in restarted._model_to_accounts.values())
    assert routed is restarted._accounts[survivor_id]
    assert sources["removed"].is_file()
    assert _parsed(sources["removed"]) == {"refreshToken": "removed-token"}


@pytest.mark.asyncio
async def test_retry_recovery_cannot_erase_successful_registration(tmp_path, monkeypatch):
    manager, credentials_file, state_file, sources, entries = _direct_pool(
        tmp_path, monkeypatch, ("removed", "survivor")
    )
    new_source = tmp_path / "new.json"
    _write_json(new_source, {"refreshToken": "new-token"})
    new_entry = {"type": "json", "path": str(new_source)}
    await manager.load_credentials()
    await manager._save_state()
    manager._initialize_account = AsyncMock(return_value=True)
    removed_id = str(sources["removed"].resolve())
    original_write_entries = accounts_admin_module._write_entries
    write_count = 0

    def fail_first_rollback(candidate_manager: AccountManager, candidate_entries: list[dict[str, object]]) -> None:
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise OSError("injected rollback credentials failure")
        original_write_entries(candidate_manager, candidate_entries)

    def fail_finalize(_: str) -> None:
        raise RuntimeError("injected finalization failure")

    monkeypatch.setattr(accounts_admin_module, "_write_entries", fail_first_rollback)

    with pytest.raises(AccountDeletionRecoveryError, match="rollback is incomplete"):
        await remove_account(manager, account_label(removed_id), finalize=fail_finalize)

    await register_account(manager, {"type": "json", "path": str(new_source)})
    assert new_entry in _parsed(credentials_file)

    await remove_account(manager, account_label(removed_id))

    assert _parsed(credentials_file) == [entries[1], new_entry]
    assert tuple(manager._accounts) == (str(sources["survivor"].resolve()), str(new_source.resolve()))
    assert not accounts_admin_module._recovery_path(manager).exists()


@pytest.mark.asyncio
async def test_pending_deletion_recovery_survives_manager_recreation(tmp_path, monkeypatch):
    manager, credentials_file, state_file, sources, entries = _direct_pool(
        tmp_path, monkeypatch, ("removed", "survivor")
    )
    new_source = tmp_path / "new.json"
    _write_json(new_source, {"refreshToken": "new-token"})
    new_entry = {"type": "json", "path": str(new_source)}
    await manager.load_credentials()
    await manager._save_state()
    removed_id = str(sources["removed"].resolve())
    original_write_entries = accounts_admin_module._write_entries
    write_count = 0

    def fail_first_rollback(candidate_manager: AccountManager, candidate_entries: list[dict[str, object]]) -> None:
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise OSError("injected rollback credentials failure")
        original_write_entries(candidate_manager, candidate_entries)

    def fail_finalize(_: str) -> None:
        raise RuntimeError("injected finalization failure")

    monkeypatch.setattr(accounts_admin_module, "_write_entries", fail_first_rollback)

    with pytest.raises(AccountDeletionRecoveryError, match="rollback is incomplete"):
        await remove_account(manager, account_label(removed_id), finalize=fail_finalize)

    recovery_path = accounts_admin_module._recovery_path(manager)
    assert recovery_path.exists()
    assert recovery_path.stat().st_mode & 0o777 == 0o600

    restarted = AccountManager(str(credentials_file), str(state_file))

    def fail_restarted_recovery(candidate_entries: list[dict[str, object]]) -> None:
        if candidate_entries == entries:
            raise OSError("injected restarted recovery failure")
        original_write_entries(candidate_entries)

    monkeypatch.setattr(accounts_admin_module, "_write_entries", fail_restarted_recovery)
    with pytest.raises(AccountDeletionRecoveryError, match="credentials.json remains changed"):
        await restarted.load_credentials()
    assert _parsed(credentials_file) == [entries[1]]

    monkeypatch.setattr(accounts_admin_module, "_write_entries", original_write_entries)
    await restarted.load_credentials()

    assert _parsed(credentials_file) == entries
    assert tuple(restarted._accounts) == (removed_id, str(sources["survivor"].resolve()))

    await restarted.load_state()
    restarted._initialize_account = AsyncMock(return_value=True)

    await register_account(restarted, {"type": "json", "path": str(new_source)})

    assert _parsed(credentials_file) == [*entries, new_entry]
    assert tuple(restarted._accounts) == (
        removed_id,
        str(sources["survivor"].resolve()),
        str(new_source.resolve()),
    )
    assert not recovery_path.exists()


@pytest.mark.asyncio
async def test_successful_removal_does_not_persist_recovery_record(tmp_path, monkeypatch):
    manager, _, _, sources, _ = _direct_pool(tmp_path, monkeypatch, ("removed", "survivor"))
    await manager.load_credentials()
    persist_recovery = Mock(wraps=accounts_admin_module._persist_removal_recovery)
    monkeypatch.setattr(accounts_admin_module, "_persist_removal_recovery", persist_recovery)

    await remove_account(manager, account_label(str(sources["removed"].resolve())))

    persist_recovery.assert_not_called()


def test_recovery_record_uses_exclusive_secure_temp_file(tmp_path, monkeypatch):
    manager, _, _, _, entries = _direct_pool(tmp_path, monkeypatch, ("removed", "survivor"))
    snapshot = {"state_file": None}
    observed_modes: list[int] = []
    secure_temp = tmp_path / ".exclusive-recovery-temp"

    def secure_mkstemp(*, prefix: str, dir: Path) -> tuple[int, str]:
        assert prefix.startswith(".credentials.json.account-deletion-recovery.")
        assert dir == tmp_path
        descriptor = os.open(secure_temp, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        observed_modes.append(os.fstat(descriptor).st_mode & 0o777)
        return descriptor, str(secure_temp)

    monkeypatch.setattr(accounts_admin_module.tempfile, "mkstemp", secure_mkstemp)

    accounts_admin_module._persist_removal_recovery(manager, entries, snapshot)
    try:
        assert observed_modes == [0o600]
        assert accounts_admin_module._recovery_path(manager).exists()
    finally:
        accounts_admin_module._clear_removal_recovery(manager)


@pytest.mark.asyncio
async def test_remove_account_uses_manager_credentials_path_when_env_differs(tmp_path, monkeypatch):
    manager, credentials_file, _, sources, entries = _direct_pool(tmp_path, monkeypatch, ("removed", "survivor"))
    await manager.load_credentials()
    unrelated_credentials = tmp_path / "unrelated-credentials.json"
    _write_json(unrelated_credentials, entries)
    monkeypatch.setenv("ACCOUNTS_CONFIG_FILE", str(unrelated_credentials))

    await remove_account(manager, account_label(str(sources["removed"].resolve())))

    assert _parsed(credentials_file) == [entries[1]]
    assert _parsed(unrelated_credentials) == entries


@pytest.mark.asyncio
async def test_register_account_serializes_with_manager_lock(tmp_path, monkeypatch):
    manager, credentials_file, _, _, entries = _direct_pool(tmp_path, monkeypatch, ("existing",))
    new_source = tmp_path / "new.json"
    _write_json(new_source, {"refreshToken": "new-token"})
    await manager.load_credentials()
    manager._initialize_account = AsyncMock(return_value=True)
    attempted = asyncio.Event()
    raw_lock = manager._lock
    await raw_lock.acquire()
    monkeypatch.setattr(manager, "_lock", _ObservedLock(raw_lock, attempted))
    register_task: asyncio.Task[dict[str, object]] | None = None
    attempted_task: asyncio.Task[bool] | None = None

    try:
        register_task = asyncio.create_task(register_account(manager, {"type": "json", "path": str(new_source)}))
        attempted_task = asyncio.create_task(attempted.wait())
        done, _ = await asyncio.wait_for(
            asyncio.wait({register_task, attempted_task}, return_when=asyncio.FIRST_COMPLETED),
            timeout=1,
        )

        assert attempted_task in done, "register_account did not serialize through AccountManager._lock"
        assert register_task not in done
        assert _parsed(credentials_file) == entries

        raw_lock.release()
        await asyncio.wait_for(register_task, timeout=1)
    finally:
        if raw_lock.locked():
            raw_lock.release()
        for task in (register_task, attempted_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (register_task, attempted_task) if task is not None), return_exceptions=True
        )

    assert _parsed(credentials_file) == [*entries, {"type": "json", "path": str(new_source)}]
