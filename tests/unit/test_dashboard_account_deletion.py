"""Authenticated dashboard account deletion and metadata cleanup."""

from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import ContextManager, Iterator, Protocol

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

import kiro.dashboard as dashboard_module
from kiro.account_manager import AccountManager, account_label

JSONValue = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
Metadata = dict[str, list[tuple[object, ...]]]


class DashboardModule(Protocol):
    router: APIRouter
    _sessions: dict[str, float]

    def _db(self) -> ContextManager[sqlite3.Connection]: ...

    def initialize_dashboard_store(self) -> None: ...


@pytest.fixture
def dashboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[DashboardModule]:
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path / "dashboard"))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "dashboard-password")
    importlib.reload(dashboard_module)
    dashboard_module.initialize_dashboard_store()
    yield dashboard_module
    dashboard_module._sessions.clear()


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _manager_for_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entries: list[dict[str, str]],
) -> tuple[AccountManager, Path, Path]:
    credentials_file = tmp_path / "credentials.json"
    state_file = tmp_path / "state.json"
    _write_json(credentials_file, entries)
    monkeypatch.setenv("ACCOUNTS_CONFIG_FILE", str(credentials_file))
    manager = AccountManager(str(credentials_file), str(state_file))
    asyncio.run(manager.load_credentials())
    return manager, credentials_file, state_file


def _direct_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    names: tuple[str, ...],
) -> tuple[AccountManager, Path, Path, dict[str, Path]]:
    sources = {name: tmp_path / f"{name}.json" for name in names}
    for name, source in sources.items():
        _write_json(source, {"refreshToken": f"{name}-credential-token"})
    entries: list[dict[str, str]] = [{"type": "json", "path": str(sources[name])} for name in names]
    manager, credentials_file, state_file = _manager_for_entries(tmp_path, monkeypatch, entries)
    return manager, credentials_file, state_file, sources


def _client(dashboard: DashboardModule, manager: AccountManager) -> TestClient:
    app = FastAPI()
    app.include_router(dashboard.router)
    app.state.account_manager = manager
    return TestClient(app)


def _login(client: TestClient) -> None:
    response = client.post("/api/dashboard/login", json={"password": "dashboard-password"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def _parsed(path: Path) -> JSONValue:
    return json.loads(path.read_text(encoding="utf-8"))


def _seed_metadata(dashboard: DashboardModule, target_id: str, survivor_id: str | None = None) -> None:
    with dashboard._db() as conn:
        for account_id, updated_at in ((target_id, 10), (survivor_id, 20)):
            if account_id is None:
                continue
            conn.execute(
                "INSERT INTO account_usage(account_id, email, updated_at) VALUES (?, ?, ?)",
                (account_id, f"{updated_at}@example.test", updated_at),
            )
            conn.execute(
                "INSERT INTO rate_observations(account_id, observed_at, rpm, rejected, outcome) VALUES (?, ?, ?, ?, ?)",
                (account_id, float(updated_at), updated_at, 0, "success"),
            )
        # Two target rows exercise stale historical metadata, not just the newest sample.
        conn.execute(
            "INSERT INTO rate_observations(account_id, observed_at, rpm, rejected, outcome) VALUES (?, ?, ?, ?, ?)",
            (target_id, 1.0, 1, 1, "rate_limited"),
        )
        conn.execute(
            "INSERT INTO request_logs(created_at, route, model, status_code, latency_ms) VALUES (1, ?, ?, 200, 5)",
            ("/v1/messages", "claude-opus-5"),
        )
        conn.execute(
            "INSERT INTO key_model_usage(key_id, model, prompt_tokens, completion_tokens, requests, updated_at)"
            " VALUES (?, ?, 3, 4, 1, 1)",
            ("global-key", "claude-opus-5"),
        )


def _metadata(dashboard: DashboardModule) -> Metadata:
    queries = {
        "account_usage": "SELECT account_id, email, updated_at FROM account_usage ORDER BY account_id",
        "rate_observations": (
            "SELECT account_id, observed_at, rpm, rejected, outcome FROM rate_observations"
            " ORDER BY account_id, observed_at"
        ),
        "request_logs": ("SELECT created_at, route, model, status_code, latency_ms FROM request_logs ORDER BY id"),
        "key_model_usage": (
            "SELECT key_id, model, prompt_tokens, completion_tokens, requests, updated_at"
            " FROM key_model_usage ORDER BY key_id, model"
        ),
    }
    with dashboard._db() as conn:
        return {name: [tuple(row) for row in conn.execute(query)] for name, query in queries.items()}


def _mutation_snapshot(
    dashboard: DashboardModule, manager: AccountManager, credentials_file: Path
) -> tuple[JSONValue, tuple[str, ...], Metadata]:
    return (_parsed(credentials_file), tuple(manager._accounts), _metadata(dashboard))


def test_account_listing_requires_dashboard_auth(
    dashboard: DashboardModule, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _, _, _ = _direct_manager(tmp_path, monkeypatch, ("only",))

    with _client(dashboard, manager) as client:
        response = client.get("/api/dashboard/accounts")

    assert response.status_code == 401


def test_account_listing_uses_only_hashed_public_labels(
    dashboard: DashboardModule, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _, _, sources = _direct_manager(tmp_path, monkeypatch, ("only",))
    account_id = str(sources["only"].resolve())

    with _client(dashboard, manager) as client:
        _login(client)
        response = client.get("/api/dashboard/accounts")

    assert response.status_code == 200
    assert response.json()["accounts"][0]["id"] == account_label(account_id)
    assert account_id not in response.text
    assert str(sources["only"]) not in response.text


def test_delete_requires_dashboard_session_not_data_plane_bearer(
    dashboard: DashboardModule, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, credentials_file, _, sources = _direct_manager(tmp_path, monkeypatch, ("target", "survivor"))
    target_id = str(sources["target"].resolve())
    before = _mutation_snapshot(dashboard, manager, credentials_file)

    with _client(dashboard, manager) as client:
        response = client.delete(
            f"/api/dashboard/accounts/{account_label(target_id)}",
            headers={"Authorization": "Bearer data-plane-key"},
        )

    assert response.status_code == 401
    assert _mutation_snapshot(dashboard, manager, credentials_file) == before


def test_metadata_deletion_failure_rolls_back_pool_and_allows_retry(
    dashboard: DashboardModule, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, credentials_file, state_file, sources = _direct_manager(tmp_path, monkeypatch, ("target", "survivor"))
    target_id = str(sources["target"].resolve())
    survivor_id = str(sources["survivor"].resolve())
    _seed_metadata(dashboard, target_id, survivor_id)
    asyncio.run(manager._save_state())
    manager._dirty = False
    before = (
        _parsed(credentials_file),
        _parsed(state_file),
        tuple(manager._accounts),
        manager._dirty,
        _metadata(dashboard),
    )
    original_db = dashboard._db

    class FailingDeleteConnection:
        def __init__(self, conn: sqlite3.Connection):
            self._conn = conn

        def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
            if sql.lstrip().upper().startswith("DELETE FROM ACCOUNT_USAGE"):
                raise sqlite3.OperationalError("injected metadata deletion failure")
            return self._conn.execute(sql, parameters)

    @contextmanager
    def failing_db() -> Iterator[FailingDeleteConnection]:
        with original_db() as conn:
            yield FailingDeleteConnection(conn)

    monkeypatch.setattr(dashboard, "_db", failing_db)

    with _client(dashboard, manager) as client:
        _login(client)
        with pytest.raises(sqlite3.OperationalError, match="injected metadata deletion failure"):
            client.delete(f"/api/dashboard/accounts/{account_label(target_id)}")

        assert (
            _parsed(credentials_file),
            _parsed(state_file),
            tuple(manager._accounts),
            manager._dirty,
            _metadata(dashboard),
        ) == before

        monkeypatch.setattr(dashboard, "_db", original_db)
        retry = client.delete(f"/api/dashboard/accounts/{account_label(target_id)}")

    assert retry.status_code == 200
    assert retry.json() == {"ok": True}
    assert _parsed(credentials_file) == [{"type": "json", "path": str(sources["survivor"])}]
    assert tuple(manager._accounts) == (survivor_id,)
    state = _parsed(state_file)
    assert isinstance(state, dict)
    accounts = state["accounts"]
    assert isinstance(accounts, dict)
    assert target_id not in accounts
    assert all(row[0] != target_id for row in _metadata(dashboard)["account_usage"])
    assert all(row[0] != target_id for row in _metadata(dashboard)["rate_observations"])


def test_authenticated_delete_returns_only_ok_and_clears_target_metadata(
    dashboard: DashboardModule, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, credentials_file, _, sources = _direct_manager(tmp_path, monkeypatch, ("target", "survivor"))
    target_id = str(sources["target"].resolve())
    survivor_id = str(sources["survivor"].resolve())
    _seed_metadata(dashboard, target_id, survivor_id)
    before = _metadata(dashboard)

    with _client(dashboard, manager) as client:
        _login(client)
        response = client.delete(f"/api/dashboard/accounts/{account_label(target_id)}")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert set(response.json()) == {"ok"}
    assert target_id not in response.text
    assert str(sources["target"]) not in response.text
    assert _parsed(credentials_file) == [{"type": "json", "path": str(sources["survivor"])}]
    assert tuple(manager._accounts) == (survivor_id,)
    assert sources["target"].is_file()

    after = _metadata(dashboard)
    assert all(row[0] != target_id for row in after["account_usage"])
    assert all(row[0] != target_id for row in after["rate_observations"])
    assert after["account_usage"] == [row for row in before["account_usage"] if row[0] == survivor_id]
    assert after["rate_observations"] == [row for row in before["rate_observations"] if row[0] == survivor_id]
    assert after["request_logs"] == before["request_logs"]
    assert after["key_model_usage"] == before["key_model_usage"]


@pytest.mark.parametrize("opaque_label", ["not-a-label", "000000000000", "%2Fetc%2Fpasswd"])
def test_unknown_or_malformed_label_is_404_without_mutation(
    dashboard: DashboardModule,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opaque_label: str,
) -> None:
    manager, credentials_file, _, sources = _direct_manager(tmp_path, monkeypatch, ("target", "survivor"))
    _seed_metadata(dashboard, str(sources["target"].resolve()), str(sources["survivor"].resolve()))
    before = _mutation_snapshot(dashboard, manager, credentials_file)

    with _client(dashboard, manager) as client:
        _login(client)
        response = client.delete(f"/api/dashboard/accounts/{opaque_label}")

    assert response.status_code == 404
    assert _mutation_snapshot(dashboard, manager, credentials_file) == before


def test_repeated_delete_is_404_and_does_not_mutate_survivor(
    dashboard: DashboardModule, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, credentials_file, _, sources = _direct_manager(tmp_path, monkeypatch, ("target", "survivor"))
    target_id = str(sources["target"].resolve())
    survivor_id = str(sources["survivor"].resolve())
    _seed_metadata(dashboard, target_id, survivor_id)

    with _client(dashboard, manager) as client:
        _login(client)
        first = client.delete(f"/api/dashboard/accounts/{account_label(target_id)}")
        after_first = _mutation_snapshot(dashboard, manager, credentials_file)
        second = client.delete(f"/api/dashboard/accounts/{account_label(target_id)}")

    assert first.status_code == 200
    assert second.status_code == 404
    assert _mutation_snapshot(dashboard, manager, credentials_file) == after_first


def test_last_direct_account_is_not_deletable_and_delete_is_409_without_mutation(
    dashboard: DashboardModule, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, credentials_file, _, sources = _direct_manager(tmp_path, monkeypatch, ("only",))
    only_id = str(sources["only"].resolve())
    _seed_metadata(dashboard, only_id)
    before = _mutation_snapshot(dashboard, manager, credentials_file)

    with _client(dashboard, manager) as client:
        _login(client)
        listing = client.get("/api/dashboard/accounts")
        response = client.delete(f"/api/dashboard/accounts/{account_label(only_id)}")

    assert listing.status_code == 200
    assert listing.json()["accounts"][0]["deletable"] is False
    assert response.status_code == 409
    assert _mutation_snapshot(dashboard, manager, credentials_file) == before
    assert sources["only"].is_file()


def test_directory_scanned_account_is_not_deletable_and_delete_is_409_without_mutation(
    dashboard: DashboardModule, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scanned_directory = tmp_path / "scanned"
    scanned_directory.mkdir()
    scanned_source = scanned_directory / "scanned.json"
    direct_source = tmp_path / "direct.json"
    _write_json(scanned_source, {"refreshToken": "scanned-credential-token"})
    _write_json(direct_source, {"refreshToken": "direct-credential-token"})
    manager, credentials_file, _ = _manager_for_entries(
        tmp_path,
        monkeypatch,
        [
            {"type": "json", "path": str(scanned_directory)},
            {"type": "json", "path": str(direct_source)},
        ],
    )
    scanned_id = str(scanned_source.resolve())
    direct_id = str(direct_source.resolve())
    _seed_metadata(dashboard, scanned_id, direct_id)
    before = _mutation_snapshot(dashboard, manager, credentials_file)

    with _client(dashboard, manager) as client:
        _login(client)
        listing = client.get("/api/dashboard/accounts")
        response = client.delete(f"/api/dashboard/accounts/{account_label(scanned_id)}")

    accounts = {account["id"]: account for account in listing.json()["accounts"]}
    assert all(type(account["deletable"]) is bool for account in accounts.values())
    assert accounts[account_label(scanned_id)]["deletable"] is False
    assert accounts[account_label(direct_id)]["deletable"] is True
    assert response.status_code == 409
    assert _mutation_snapshot(dashboard, manager, credentials_file) == before
    assert scanned_source.is_file()
    assert direct_source.is_file()


def test_every_direct_account_listing_has_required_boolean_deletable(
    dashboard: DashboardModule, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _, _, _ = _direct_manager(tmp_path, monkeypatch, ("first", "second"))

    with _client(dashboard, manager) as client:
        _login(client)
        response = client.get("/api/dashboard/accounts")

    assert response.status_code == 200
    assert response.json()["accounts"]
    assert all(type(account["deletable"]) is bool for account in response.json()["accounts"])
    assert all(account["deletable"] is True for account in response.json()["accounts"])
