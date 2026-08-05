"""Contract tests for quota-reset-aware quarantine and the depleted report state.

Two defects motivated these. A 402 quarantine ran for a fixed
ACCOUNT_QUOTA_QUARANTINE (6h) and then expired regardless of the actual monthly
reset: measured live, three accounts came back ~34-40h early at 1000/1000, able
only to answer 402 again. And once the quarantine lapsed those accounts reported
"available" on the dashboard, so a spent account read as ready.
"""

import importlib
import time
from unittest.mock import MagicMock

import pytest

from kiro.account_errors import ErrorType
from kiro.account_manager import Account, AccountManager, account_routing_state
from kiro.config import (
    ACCOUNT_QUOTA_QUARANTINE,
    ACCOUNT_QUOTA_QUARANTINE_MAX,
    ACCOUNT_QUOTA_RESET_MARGIN,
)

DAY = 86400.0


def _manager(tmp_path, account_ids=("/creds/account0.json",)) -> AccountManager:
    manager = AccountManager(
        credentials_file=str(tmp_path / "credentials.json"), state_file=str(tmp_path / "state.json")
    )
    for account_id in account_ids:
        account = Account(id=account_id)
        account.auth_manager = MagicMock()
        account.models_cached_at = time.time()
        manager._accounts[account_id] = account
    return manager


def _depleted_account(**overrides) -> Account:
    account = Account(id="/creds/account.json")
    account.auth_manager = MagicMock()
    account.quota_headroom = 0.0
    account.quota_overage_enabled = False
    for key, value in overrides.items():
        setattr(account, key, value)
    return account


class TestQuarantineEndsWithTheAllowance:
    """The quarantine must wait for the reset, not for a fixed interval."""

    @pytest.mark.asyncio
    async def test_quarantine_runs_to_the_reported_reset(self, tmp_path):
        manager = _manager(tmp_path)
        account_id = "/creds/account0.json"
        reset_at = time.time() + 3 * DAY
        manager._accounts[account_id].quota_resets_at = reset_at

        await manager.report_failure(
            account_id, "claude-sonnet-4-5", ErrorType.RECOVERABLE, 402, "MONTHLY_REQUEST_COUNT"
        )

        # The fixed window would have released the account ~2.75 days early.
        assert manager._accounts[account_id].quota_exhausted_until == pytest.approx(
            reset_at + ACCOUNT_QUOTA_RESET_MARGIN, abs=2
        )

    @pytest.mark.asyncio
    async def test_account_stays_excluded_past_the_old_fixed_window(self, tmp_path):
        manager = _manager(tmp_path, ("/creds/account0.json", "/creds/account1.json"))
        spent = "/creds/account0.json"
        manager._accounts[spent].quota_resets_at = time.time() + 2 * DAY

        await manager.report_failure(spent, "claude-sonnet-4-5", ErrorType.RECOVERABLE, 402, "MONTHLY_REQUEST_COUNT")

        # Simulate the moment the fixed quarantine used to lapse.
        later = time.time() + ACCOUNT_QUOTA_QUARANTINE + 60
        state, remaining = account_routing_state(manager._accounts[spent], later)

        assert state == "quota_exhausted"
        assert remaining > 0

    @pytest.mark.asyncio
    async def test_unknown_reset_falls_back_to_the_fixed_window(self, tmp_path):
        manager = _manager(tmp_path)
        account_id = "/creds/account0.json"
        assert manager._accounts[account_id].quota_resets_at == 0.0

        before = time.time()
        await manager.report_failure(
            account_id, "claude-sonnet-4-5", ErrorType.RECOVERABLE, 402, "MONTHLY_REQUEST_COUNT"
        )

        remaining = manager._accounts[account_id].quota_exhausted_until - before
        assert remaining == pytest.approx(ACCOUNT_QUOTA_QUARANTINE, abs=2)

    @pytest.mark.asyncio
    async def test_past_reset_does_not_shorten_the_quarantine(self, tmp_path):
        # A stale row can report a reset that has already happened while the
        # account still answers 402. Trusting it would mean an instant retry.
        manager = _manager(tmp_path)
        account_id = "/creds/account0.json"
        manager._accounts[account_id].quota_resets_at = time.time() - 5 * DAY

        before = time.time()
        await manager.report_failure(
            account_id, "claude-sonnet-4-5", ErrorType.RECOVERABLE, 402, "MONTHLY_REQUEST_COUNT"
        )

        remaining = manager._accounts[account_id].quota_exhausted_until - before
        assert remaining == pytest.approx(ACCOUNT_QUOTA_QUARANTINE, abs=2)

    @pytest.mark.asyncio
    async def test_absurd_reset_is_capped(self, tmp_path):
        # A malformed date must not translate into a permanent exclusion; past
        # the cap the account is retried and the upstream 402 decides again.
        manager = _manager(tmp_path)
        account_id = "/creds/account0.json"
        manager._accounts[account_id].quota_resets_at = time.time() + 3650 * DAY

        before = time.time()
        await manager.report_failure(
            account_id, "claude-sonnet-4-5", ErrorType.RECOVERABLE, 402, "MONTHLY_REQUEST_COUNT"
        )

        remaining = manager._accounts[account_id].quota_exhausted_until - before
        assert remaining == pytest.approx(ACCOUNT_QUOTA_QUARANTINE_MAX, abs=2)

    @pytest.mark.asyncio
    async def test_success_still_clears_the_quarantine(self, tmp_path):
        # A served request proves the quota is back even if the reset date said
        # otherwise, so the reset-derived window must not outlive that evidence.
        manager = _manager(tmp_path)
        account_id = "/creds/account0.json"
        manager._accounts[account_id].quota_resets_at = time.time() + 10 * DAY

        await manager.report_failure(
            account_id, "claude-sonnet-4-5", ErrorType.RECOVERABLE, 402, "MONTHLY_REQUEST_COUNT"
        )
        assert manager._accounts[account_id].quota_exhausted_until > time.time()

        await manager.report_success(account_id, "claude-sonnet-4-5")

        assert manager._accounts[account_id].quota_exhausted_until == 0.0


class TestDepletedIsReportedNotHidden:
    """A spent allowance must not read as "available"."""

    def test_spent_allowance_is_reported_as_depleted(self):
        account = _depleted_account(quota_resets_at=time.time() + 2 * DAY)

        state, remaining = account_routing_state(account)

        assert state == "quota_depleted"
        assert remaining > 0

    def test_depleted_without_a_reset_date_reports_zero_wait(self):
        state, remaining = account_routing_state(_depleted_account())

        assert state == "quota_depleted"
        assert remaining == 0

    def test_overage_enabled_account_stays_available(self):
        # At 100% with overage billing, the account really can still serve.
        account = _depleted_account(quota_overage_enabled=True)

        state, _ = account_routing_state(account)

        assert state == "available"

    def test_unknown_overage_stays_available(self):
        # "UNKNOWN" or an unpolled account must not be labelled done on a guess.
        account = _depleted_account(quota_overage_enabled=None)

        state, _ = account_routing_state(account)

        assert state == "available"

    def test_unpolled_account_stays_available(self):
        account = _depleted_account(quota_headroom=None, quota_overage_enabled=False)

        state, _ = account_routing_state(account)

        assert state == "available"

    def test_account_with_headroom_stays_available(self):
        account = _depleted_account(quota_headroom=0.42)

        state, _ = account_routing_state(account)

        assert state == "available"

    @pytest.mark.parametrize(
        "field,value",
        [
            ("quota_exhausted_until", time.time() + 3600),
            ("suspended_until", time.time() + 3600),
            ("rate_limited_until", time.time() + 30),
        ],
        ids=["quota_exhausted", "suspended", "rate_limited"],
    )
    def test_real_exclusions_outrank_the_report_state(self, field, value):
        # quota_depleted is the weakest classification: it must never mask a
        # condition that actually removes the account from rotation.
        account = _depleted_account(**{field: value})

        state, _ = account_routing_state(account)

        assert state != "quota_depleted"

    def test_uninitialized_outranks_the_report_state(self):
        account = _depleted_account()
        account.auth_manager = None

        state, _ = account_routing_state(account)

        assert state == "uninitialized"


class TestDepletedAccountsAreExcluded:
    """A spent allowance is excluded, with one deliberate escape hatch."""

    def _deplete(self, manager: AccountManager, account_id: str) -> Account:
        account = manager._accounts[account_id]
        account.quota_headroom = 0.0
        account.quota_overage_enabled = False
        account.quota_resets_at = time.time() + DAY
        return account

    @pytest.mark.asyncio
    async def test_depleted_account_is_skipped_for_a_healthy_one(self, tmp_path):
        manager = _manager(tmp_path, ("/creds/spent.json", "/creds/healthy.json"))
        self._deplete(manager, "/creds/spent.json")
        manager._accounts["/creds/healthy.json"].quota_headroom = 0.5

        # Repeated draws: the order is randomized, so a single call could pick the
        # healthy account by luck rather than by the exclusion.
        for _ in range(40):
            selected = await manager.get_next_account("claude-sonnet-4-5")
            assert selected is not None
            assert selected.id == "/creds/healthy.json"

    @pytest.mark.asyncio
    async def test_every_depleted_account_is_skipped(self, tmp_path):
        manager = _manager(tmp_path, (f"/creds/spent{i}.json" for i in range(4)))
        for account_id in list(manager._accounts):
            self._deplete(manager, account_id)
        manager._accounts["/creds/healthy.json"] = Account(id="/creds/healthy.json")
        manager._accounts["/creds/healthy.json"].auth_manager = MagicMock()
        manager._accounts["/creds/healthy.json"].models_cached_at = time.time()
        manager._accounts["/creds/healthy.json"].quota_headroom = 0.1

        for _ in range(40):
            selected = await manager.get_next_account("claude-sonnet-4-5")
            assert selected is not None
            assert selected.id == "/creds/healthy.json"

    @pytest.mark.asyncio
    async def test_overage_enabled_account_at_zero_is_not_excluded(self, tmp_path):
        # 100% with overage billing on can still serve, so it stays eligible.
        manager = _manager(tmp_path, ("/creds/overage.json", "/creds/spent.json"))
        self._deplete(manager, "/creds/spent.json")
        overage = self._deplete(manager, "/creds/overage.json")
        overage.quota_overage_enabled = True

        for _ in range(30):
            selected = await manager.get_next_account("claude-sonnet-4-5")
            assert selected is not None
            assert selected.id == "/creds/overage.json"

    @pytest.mark.asyncio
    async def test_unpolled_account_is_not_excluded(self, tmp_path):
        # No reading is not evidence of a spent quota.
        manager = _manager(tmp_path, ("/creds/unpolled.json", "/creds/spent.json"))
        self._deplete(manager, "/creds/spent.json")
        manager._accounts["/creds/unpolled.json"].quota_headroom = None

        for _ in range(30):
            selected = await manager.get_next_account("claude-sonnet-4-5")
            assert selected is not None
            assert selected.id == "/creds/unpolled.json"

    @pytest.mark.asyncio
    async def test_all_depleted_falls_back_rather_than_emptying_the_pool(self, tmp_path):
        # The escape hatch: this exclusion is inferred from telemetry, so a stalled
        # usage poll must not turn into "no accounts available". Let the upstream
        # answer instead.
        manager = _manager(tmp_path, ("/creds/spent0.json", "/creds/spent1.json"))
        for account_id in list(manager._accounts):
            self._deplete(manager, account_id)

        selected = await manager.get_next_account("claude-sonnet-4-5")

        assert selected is not None
        assert selected.id in manager._accounts

    @pytest.mark.asyncio
    async def test_last_resort_still_honors_upstream_backed_exclusions(self, tmp_path):
        # The fallback lifts only the telemetry-derived exclusion. A suspension is
        # an upstream fact and must survive it.
        manager = _manager(tmp_path, ("/creds/spent.json", "/creds/banned.json"))
        self._deplete(manager, "/creds/spent.json")
        banned = self._deplete(manager, "/creds/banned.json")
        banned.suspended_until = time.time() + 3600

        for _ in range(30):
            selected = await manager.get_next_account("claude-sonnet-4-5")
            assert selected is not None
            assert selected.id == "/creds/spent.json"

    @pytest.mark.asyncio
    async def test_last_resort_does_not_revive_a_quota_quarantine(self, tmp_path):
        manager = _manager(tmp_path, ("/creds/spent.json", "/creds/quarantined.json"))
        self._deplete(manager, "/creds/spent.json")
        quarantined = self._deplete(manager, "/creds/quarantined.json")
        quarantined.quota_exhausted_until = time.time() + 3600

        for _ in range(30):
            selected = await manager.get_next_account("claude-sonnet-4-5")
            assert selected is not None
            assert selected.id == "/creds/spent.json"

    @pytest.mark.asyncio
    async def test_exhausted_pool_still_returns_none(self, tmp_path):
        # The fallback must not resurrect an account excluded for a real reason.
        manager = _manager(tmp_path, ("/creds/a.json", "/creds/b.json"))
        for account_id in list(manager._accounts):
            manager._accounts[account_id].suspended_until = time.time() + 3600

        assert await manager.get_next_account("claude-sonnet-4-5") is None

    def test_pool_description_reports_it_as_an_exclusion(self, tmp_path):
        manager = _manager(tmp_path, ("/creds/account0.json", "/creds/other.json"))
        self._deplete(manager, "/creds/account0.json")

        description = manager.describe_pool_state()

        assert "monthly quota spent" in description
        # The old wording advertised it as a candidate; it is not one any more.
        assert "still tried" not in description

    def test_both_quota_states_read_the_same_way(self, tmp_path):
        # Same operational fact, different evidence: the phrasing stays parallel so
        # neither reads as the milder condition.
        manager = _manager(tmp_path, ("/creds/spent.json", "/creds/exhausted.json"))
        self._deplete(manager, "/creds/spent.json")
        manager._accounts["/creds/exhausted.json"].quota_exhausted_until = time.time() + DAY

        description = manager.describe_pool_state()

        assert "monthly quota spent, excluded for" in description
        assert "monthly quota exhausted, excluded for" in description

    def test_pool_description_names_a_suspension(self, tmp_path):
        # Previously a suspended account fell through to "available" here, which
        # pointed an operator at the pool instead of the account.
        manager = _manager(tmp_path)
        manager._accounts["/creds/account0.json"].suspended_until = time.time() + 3600

        description = manager.describe_pool_state()

        assert "suspended upstream" in description
        assert "available" not in description


class TestQuotaPeriodPlumbing:
    """The reset date and overage flag have to reach the manager."""

    @pytest.fixture
    def dashboard(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
        module = importlib.reload(importlib.import_module("kiro.dashboard"))
        module.initialize_dashboard_store()
        return module

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
        module = importlib.reload(importlib.import_module("kiro.store"))
        module.initialize()
        return module

    def test_reset_parsed_from_number_and_string(self, dashboard):
        assert dashboard._reset_at_from_usage({"nextDateReset": 1785542400.0}) == 1785542400.0
        # The upstream and the TEXT column have both produced string values.
        assert dashboard._reset_at_from_usage({"nextDateReset": "1785542400.0"}) == 1785542400.0

    @pytest.mark.parametrize(
        "usage",
        [
            {},
            {"nextDateReset": None},
            {"nextDateReset": ""},
            {"nextDateReset": "soon"},
            {"nextDateReset": 0},
            {"nextDateReset": -5},
            {"nextDateReset": True},
            {"nextDateReset": 1785542400.0, "error": "boom"},
        ],
        ids=["empty", "null", "blank", "text", "zero", "negative", "bool", "failed-read"],
    )
    def test_unusable_reset_is_none(self, dashboard, usage):
        assert dashboard._reset_at_from_usage(usage) is None

    @pytest.mark.parametrize(
        "status,expected",
        [("DISABLED", False), ("ENABLED", True), ("disabled", False), (" enabled ", True)],
    )
    def test_overage_status_maps_to_bool(self, dashboard, status, expected):
        assert dashboard._overage_enabled_from_usage({"overageStatus": status}) is expected

    @pytest.mark.parametrize(
        "usage",
        [{}, {"overageStatus": None}, {"overageStatus": "UNKNOWN"}, {"overageStatus": "DISABLED", "error": "boom"}],
        ids=["empty", "null", "unknown", "failed-read"],
    )
    def test_inconclusive_overage_is_none(self, dashboard, usage):
        assert dashboard._overage_enabled_from_usage(usage) is None

    @pytest.mark.asyncio
    async def test_usage_refresh_feeds_the_quota_period(self, dashboard, tmp_path, monkeypatch):
        manager = _manager(tmp_path)
        reset_at = time.time() + 5 * DAY

        async def fake_refresh(account):
            return {
                "currentUsage": 1000.0,
                "usageLimit": 1000.0,
                "nextDateReset": reset_at,
                "overageStatus": "DISABLED",
                "error": None,
            }

        monkeypatch.setattr(dashboard, "refresh_account_usage", fake_refresh)

        await dashboard.refresh_all_account_usage(manager)

        account = manager._accounts["/creds/account0.json"]
        assert account.quota_resets_at == pytest.approx(reset_at)
        assert account.quota_overage_enabled is False
        # And the combination is now reportable rather than reading as ready.
        assert account_routing_state(account)[0] == "quota_depleted"

    def test_set_quota_period_ignores_bad_input(self, tmp_path):
        manager = _manager(tmp_path)
        account_id = "/creds/account0.json"

        manager.set_quota_period(account_id, None, None)
        assert manager._accounts[account_id].quota_resets_at == 0.0

        manager.set_quota_period(account_id, -1.0, False)
        assert manager._accounts[account_id].quota_resets_at == 0.0
        assert manager._accounts[account_id].quota_overage_enabled is False

        # An unknown account is ignored rather than raising.
        manager.set_quota_period("/creds/missing.json", 1.0, True)

    def test_load_quota_period_reads_persisted_rows(self, store):
        with store.connection() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS account_usage (
                    account_id TEXT PRIMARY KEY, next_date_reset TEXT, overage_status TEXT, error TEXT
                )"""
            )
            conn.executemany(
                "INSERT INTO account_usage(account_id, next_date_reset, overage_status, error) VALUES (?, ?, ?, NULL)",
                [
                    ("/creds/good.json", "1785542400.0", "DISABLED"),
                    ("/creds/blank.json", "", "ENABLED"),
                    ("/creds/nothing.json", "", "UNKNOWN"),
                ],
            )

        period = store.load_quota_period()

        assert period["/creds/good.json"] == (1785542400.0, False)
        # A blank date still carries a usable overage flag.
        assert period["/creds/blank.json"] == (None, True)
        # A row with neither fact is omitted rather than stored as unknown.
        assert "/creds/nothing.json" not in period

    def test_missing_usage_table_is_not_an_error(self, store):
        assert store.load_quota_period() == {}

    @pytest.mark.asyncio
    async def test_load_state_seeds_the_quota_period(self, store, tmp_path):
        reset_at = time.time() + 4 * DAY
        with store.connection() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS account_usage (
                    account_id TEXT PRIMARY KEY, next_date_reset TEXT, overage_status TEXT, error TEXT
                )"""
            )
            conn.execute(
                "INSERT INTO account_usage(account_id, next_date_reset, overage_status, error) VALUES (?, ?, ?, NULL)",
                ("/creds/account0.json", str(reset_at), "DISABLED"),
            )

        manager = _manager(tmp_path)

        # Without seeding, a restart mid-quarantine would fall back to the fixed
        # window and re-admit an account whose quota is still spent.
        await manager.load_state()

        account = manager._accounts["/creds/account0.json"]
        assert account.quota_resets_at == pytest.approx(reset_at)
        assert account.quota_overage_enabled is False


class TestErroredRowsAreNotRevived:
    """A failed poll must not leave stale facts for a restart to pick up."""

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
        module = importlib.reload(importlib.import_module("kiro.store"))
        module.initialize()
        return module

    def _usage_table(self, store, rows):
        with store.connection() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS account_usage (
                    account_id TEXT PRIMARY KEY, next_date_reset TEXT, overage_status TEXT, error TEXT
                )"""
            )
            conn.executemany(
                "INSERT INTO account_usage(account_id, next_date_reset, overage_status, error) VALUES (?, ?, ?, ?)",
                rows,
            )

    def test_errored_row_is_skipped(self, store):
        # A failed refresh rewrites only updated_at and error, so the row keeps a
        # reset date the current poll could not confirm. Reading it would
        # quarantine to an obsolete date instead of the safe fixed window.
        self._usage_table(
            store,
            [
                ("/creds/ok.json", "1785542400.0", "DISABLED", None),
                ("/creds/stale.json", "1785542400.0", "DISABLED", "Account is not initialized"),
            ],
        )

        period = store.load_quota_period()

        assert "/creds/ok.json" in period
        assert "/creds/stale.json" not in period

    @pytest.mark.asyncio
    async def test_restart_after_failed_poll_uses_the_fixed_window(self, store, tmp_path):
        self._usage_table(store, [("/creds/account0.json", str(time.time() + 20 * DAY), "DISABLED", "boom")])

        manager = _manager(tmp_path)
        await manager.load_state()

        assert manager._accounts["/creds/account0.json"].quota_resets_at == 0.0

        before = time.time()
        await manager.report_failure(
            "/creds/account0.json", "claude-sonnet-4-5", ErrorType.RECOVERABLE, 402, "MONTHLY_REQUEST_COUNT"
        )

        remaining = manager._accounts["/creds/account0.json"].quota_exhausted_until - before
        assert remaining == pytest.approx(ACCOUNT_QUOTA_QUARANTINE, abs=2)

    @pytest.mark.asyncio
    async def test_failed_poll_clears_a_live_reset(self, tmp_path, monkeypatch):
        # The in-process path has to forget too, not just the seeding path.
        monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
        dashboard = importlib.reload(importlib.import_module("kiro.dashboard"))
        dashboard.initialize_dashboard_store()

        manager = _manager(tmp_path)
        account = manager._accounts["/creds/account0.json"]
        account.quota_resets_at = time.time() + 20 * DAY
        account.quota_overage_enabled = False

        async def failing_refresh(acct):
            return {"updatedAt": 1, "error": "boom"}

        monkeypatch.setattr(dashboard, "refresh_account_usage", failing_refresh)

        await dashboard.refresh_all_account_usage(manager)

        assert account.quota_resets_at == 0.0
        assert account.quota_overage_enabled is None


class TestNonFiniteResetIsRejected:
    """inf/nan survive float() but are not dates and break JSON encoding."""

    @pytest.fixture
    def dashboard(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
        module = importlib.reload(importlib.import_module("kiro.dashboard"))
        module.initialize_dashboard_store()
        return module

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
        module = importlib.reload(importlib.import_module("kiro.store"))
        module.initialize()
        return module

    @pytest.mark.parametrize("raw", ["inf", "-inf", "nan", float("inf"), float("nan")])
    def test_parser_rejects_non_finite(self, dashboard, raw):
        assert dashboard._reset_at_from_usage({"nextDateReset": raw}) is None

    def test_setter_rejects_non_finite(self, tmp_path):
        manager = _manager(tmp_path)
        account_id = "/creds/account0.json"

        manager.set_quota_period(account_id, float("inf"), False)
        assert manager._accounts[account_id].quota_resets_at == 0.0

        manager.set_quota_period(account_id, float("nan"), False)
        assert manager._accounts[account_id].quota_resets_at == 0.0

    def test_store_rejects_non_finite(self, store):
        with store.connection() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS account_usage (
                    account_id TEXT PRIMARY KEY, next_date_reset TEXT, overage_status TEXT, error TEXT
                )"""
            )
            conn.execute(
                "INSERT INTO account_usage(account_id, next_date_reset, overage_status, error) VALUES (?,?,?,NULL)",
                ("/creds/inf.json", "inf", "DISABLED"),
            )

        assert store.load_quota_period()["/creds/inf.json"] == (None, False)

    def test_account_view_stays_json_serializable(self, dashboard, tmp_path):
        # The regression this guards: one non-finite value fails encoding for the
        # whole /api/dashboard/accounts response, not just its own account.
        import json as json_module

        manager = _manager(tmp_path)
        account_id = "/creds/account0.json"
        manager.set_quota_period(account_id, float("inf"), False)

        view = dashboard._account_view(manager._accounts[account_id])

        assert json_module.dumps(view, allow_nan=False)
        assert view["quotaResetsAt"] is None

    @pytest.mark.asyncio
    async def test_infinite_reset_does_not_pin_the_quarantine_to_the_cap(self, tmp_path):
        manager = _manager(tmp_path)
        account_id = "/creds/account0.json"
        manager.set_quota_period(account_id, float("inf"), False)

        before = time.time()
        await manager.report_failure(
            account_id, "claude-sonnet-4-5", ErrorType.RECOVERABLE, 402, "MONTHLY_REQUEST_COUNT"
        )

        remaining = manager._accounts[account_id].quota_exhausted_until - before
        assert remaining == pytest.approx(ACCOUNT_QUOTA_QUARANTINE, abs=2)
