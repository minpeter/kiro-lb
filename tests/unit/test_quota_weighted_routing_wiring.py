"""Contract tests for how quota telemetry reaches the router.

Weighted routing is only as good as the headroom feeding it. Two seams have to
hold: the usage refresh must update the router (not just the dashboard rows),
and a restart or blue/green handoff must recover a weight before the first poll
arrives - otherwise the pool routes blind for up to USAGE_REFRESH_INTERVAL_SECONDS
and starvation returns through the back door.
"""

import importlib
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro.account_manager import Account, AccountManager


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    module = importlib.reload(importlib.import_module("kiro.dashboard"))
    module.initialize_dashboard_store()
    return module


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    module = importlib.reload(importlib.import_module("kiro.store"))
    module.initialize()
    return module


def _manager(tmp_path, account_ids: list[str]) -> AccountManager:
    manager = AccountManager()
    for account_id in account_ids:
        account = Account(id=account_id)
        account.auth_manager = MagicMock()
        account.models_cached_at = time.time()
        manager._accounts[account_id] = account
    return manager


class TestHeadroomFromUsage:
    """The ratio conversion must refuse to guess."""

    def test_partial_usage_becomes_remaining_fraction(self, dashboard):
        headroom = dashboard._headroom_from_usage({"currentUsage": 92.97, "usageLimit": 1000.0})

        assert headroom == pytest.approx(0.90703)

    def test_full_usage_is_zero_not_none(self, dashboard):
        assert dashboard._headroom_from_usage({"currentUsage": 1000.0, "usageLimit": 1000.0}) == 0.0

    def test_failed_reading_is_unknown(self, dashboard):
        usage = {"currentUsage": 5.0, "usageLimit": 100.0, "error": "Account is not initialized"}

        assert dashboard._headroom_from_usage(usage) is None

    @pytest.mark.parametrize(
        "usage",
        [
            {},
            {"currentUsage": None, "usageLimit": 100.0},
            {"currentUsage": 5.0, "usageLimit": None},
            {"currentUsage": 5.0, "usageLimit": 0},
        ],
        ids=["empty", "no-current", "no-limit", "zero-limit"],
    )
    def test_unusable_readings_are_unknown(self, dashboard, usage):
        assert dashboard._headroom_from_usage(usage) is None

    def test_overage_beyond_limit_clamps_to_zero(self, dashboard):
        # Overage can push usage past the limit; a negative weight would invert
        # the ordering and hand the drained account the most traffic.
        assert dashboard._headroom_from_usage({"currentUsage": 1200.0, "usageLimit": 1000.0}) == 0.0


class TestUsageRefreshFeedsRouter:
    """A refresh that updates only the dashboard leaves routing stale."""

    @pytest.mark.asyncio
    async def test_refresh_updates_routing_weight(self, dashboard, tmp_path, monkeypatch):
        manager = _manager(tmp_path, ["/creds/account0.json"])

        async def fake_refresh(account):
            return {"currentUsage": 250.0, "usageLimit": 1000.0, "error": None}

        monkeypatch.setattr(dashboard, "refresh_account_usage", fake_refresh)

        await dashboard.refresh_all_account_usage(manager)

        assert manager._accounts["/creds/account0.json"].quota_headroom == pytest.approx(0.75)

    @pytest.mark.asyncio
    async def test_failed_refresh_resets_weight_to_unknown(self, dashboard, tmp_path, monkeypatch):
        manager = _manager(tmp_path, ["/creds/account0.json"])
        manager._accounts["/creds/account0.json"].quota_headroom = 0.9

        async def fake_refresh(account):
            return {"updatedAt": 1, "error": "boom"}

        monkeypatch.setattr(dashboard, "refresh_account_usage", fake_refresh)

        await dashboard.refresh_all_account_usage(manager)

        # Unknown, not zero: a failed poll says nothing about the quota, and
        # zeroing it would push the account to the bottom of every draw.
        assert manager._accounts["/creds/account0.json"].quota_headroom is None

    @pytest.mark.asyncio
    async def test_weight_update_failure_does_not_break_refresh(self, dashboard, tmp_path, monkeypatch):
        manager = _manager(tmp_path, ["/creds/account0.json"])
        manager.set_quota_headroom = MagicMock(side_effect=RuntimeError("nope"))

        async def fake_refresh(account):
            return {"currentUsage": 1.0, "usageLimit": 10.0, "error": None}

        monkeypatch.setattr(dashboard, "refresh_account_usage", fake_refresh)

        results = await dashboard.refresh_all_account_usage(manager)

        assert len(results) == 1


class TestHeadroomSeeding:
    """Restart and handoff must not route blind until the first poll."""

    def test_load_quota_headroom_reads_persisted_rows(self, store):
        with store.connection() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS account_usage (
                    account_id TEXT PRIMARY KEY, current_usage REAL, usage_limit REAL, error TEXT
                )"""
            )
            conn.executemany(
                "INSERT INTO account_usage(account_id, current_usage, usage_limit, error) VALUES (?, ?, ?, ?)",
                [
                    ("/creds/good.json", 100.0, 1000.0, None),
                    ("/creds/failed.json", 10.0, 100.0, "boom"),
                    ("/creds/zero-limit.json", 10.0, 0.0, None),
                ],
            )

        headroom = store.load_quota_headroom()

        assert headroom == {"/creds/good.json": pytest.approx(0.9)}

    def test_missing_usage_table_is_not_an_error(self, store):
        # A fresh database has no dashboard tables yet; seeding must degrade to
        # "unknown for everyone" rather than failing state load.
        assert store.load_quota_headroom() == {}

    @pytest.mark.asyncio
    async def test_load_state_seeds_router_weights(self, store, tmp_path):
        with store.connection() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS account_usage (
                    account_id TEXT PRIMARY KEY, current_usage REAL, usage_limit REAL, error TEXT
                )"""
            )
            conn.execute(
                "INSERT INTO account_usage(account_id, current_usage, usage_limit, error) VALUES (?, ?, ?, NULL)",
                ("/creds/account0.json", 200.0, 1000.0),
            )

        manager = _manager(tmp_path, ["/creds/account0.json", "/creds/account1.json"])
        assert manager._accounts["/creds/account0.json"].quota_headroom is None

        await manager.load_state()

        assert manager._accounts["/creds/account0.json"].quota_headroom == pytest.approx(0.8)
        # No row for the second account: unknown, so it keeps the neutral weight.
        assert manager._accounts["/creds/account1.json"].quota_headroom is None

    @pytest.mark.asyncio
    async def test_handoff_reload_reseeds_weights(self, store, tmp_path):
        with store.connection() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS account_usage (
                    account_id TEXT PRIMARY KEY, current_usage REAL, usage_limit REAL, error TEXT
                )"""
            )
            conn.execute(
                "INSERT INTO account_usage(account_id, current_usage, usage_limit, error) VALUES (?, ?, ?, NULL)",
                ("/creds/account0.json", 500.0, 1000.0),
            )

        manager = _manager(tmp_path, ["/creds/account0.json"])
        # reload_durable_state() rebuilds the pool from the store, which would
        # otherwise drop every weight on each blue/green flip.
        manager._load_credentials_unlocked = AsyncMock()

        await manager.reload_durable_state()
        manager._accounts["/creds/account0.json"] = Account(id="/creds/account0.json")
        await manager.load_state()

        assert manager._accounts["/creds/account0.json"].quota_headroom == pytest.approx(0.5)


class TestRegistrationSeedsWeight:
    """A newly registered account must not route at the neutral weight."""

    @pytest.mark.asyncio
    async def test_registration_poll_sets_routing_weight(self, dashboard, tmp_path, monkeypatch):
        manager = _manager(tmp_path, ["/creds/new.json"])
        assert manager._accounts["/creds/new.json"].quota_headroom is None

        async def fake_refresh(account):
            return {"currentUsage": 20.0, "usageLimit": 1000.0, "error": None}

        monkeypatch.setattr(dashboard, "refresh_account_usage", fake_refresh)

        result = await dashboard.prime_registered_account_usage(manager, {"accountKey": "/creds/new.json"})

        # A fresh account is usually the emptiest in the pool; leaving it at the
        # unknown weight until the next bulk refresh understates it.
        assert manager._accounts["/creds/new.json"].quota_headroom == pytest.approx(0.98)
        # The internal account key must never reach the client.
        assert "accountKey" not in result

    @pytest.mark.asyncio
    async def test_registration_still_succeeds_when_weight_update_fails(self, dashboard, tmp_path, monkeypatch):
        manager = _manager(tmp_path, ["/creds/new.json"])
        manager.set_quota_headroom = MagicMock(side_effect=RuntimeError("nope"))

        async def fake_refresh(account):
            return {"currentUsage": 1.0, "usageLimit": 10.0, "error": None}

        monkeypatch.setattr(dashboard, "refresh_account_usage", fake_refresh)

        result = await dashboard.prime_registered_account_usage(manager, {"accountKey": "/creds/new.json", "id": "x"})

        assert result == {"id": "x"}

    @pytest.mark.asyncio
    async def test_uninitialized_registration_is_left_unknown(self, dashboard, tmp_path, monkeypatch):
        manager = _manager(tmp_path, ["/creds/new.json"])
        manager._accounts["/creds/new.json"].auth_manager = None

        async def fail(account):  # pragma: no cover - must not be reached
            raise AssertionError("must not poll an uninitialized account")

        monkeypatch.setattr(dashboard, "refresh_account_usage", fail)

        await dashboard.prime_registered_account_usage(manager, {"accountKey": "/creds/new.json"})

        assert manager._accounts["/creds/new.json"].quota_headroom is None
