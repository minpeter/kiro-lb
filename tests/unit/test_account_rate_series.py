"""Contract tests for the per-account request-rate series.

The dashboard request log records the client-facing result, so a 429 that
failover recovered from is filed as a 200 and carries no account attribution.
This series is the only place a rate-limit burst is visible per account.
"""

import time
from typing import Any

import pytest

from kiro.account_errors import ErrorType
from kiro.account_manager import Account, AccountManager, RateObservation, account_label
from kiro.config import RATE_ESTIMATE_WINDOW_SECONDS, RATE_WINDOW_SECONDS


@pytest.fixture
def manager(tmp_path) -> AccountManager:
    instance = AccountManager(
        credentials_file=str(tmp_path / "credentials.json"),
        state_file=str(tmp_path / "state.json"),
    )
    for index in range(2):
        account_id = f"/creds/account{index}.json"
        instance._accounts[account_id] = Account(id=account_id)
    return instance


def _series_for(payload: dict[str, Any], account_id: str) -> dict[str, Any]:
    label = account_label(account_id)
    return next(entry for entry in payload["accounts"] if entry["account"] == label)


@pytest.mark.asyncio
async def test_success_and_rate_limit_are_counted_separately(manager):
    await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")
    await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")
    await manager.report_failure(
        "/creds/account0.json", "claude-sonnet-4-5", ErrorType.RECOVERABLE, 429, "USER_REQUEST_RATE_EXCEEDED"
    )

    series = _series_for(manager.request_rate_series(60, 15), "/creds/account0.json")

    assert sum(series["success"]) == 2
    assert sum(series["rateLimited"]) == 1
    assert sum(series["failure"]) == 0


@pytest.mark.asyncio
async def test_quota_exhaustion_is_not_reported_as_a_rate_limit(manager):
    await manager.report_failure(
        "/creds/account0.json", "claude-sonnet-4-5", ErrorType.RECOVERABLE, 402, "MONTHLY_REQUEST_COUNT"
    )

    series = _series_for(manager.request_rate_series(60, 15), "/creds/account0.json")

    assert sum(series["rateLimited"]) == 0
    assert sum(series["failure"]) == 1


@pytest.mark.asyncio
async def test_events_are_attributed_to_their_own_account(manager):
    await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")
    await manager.report_failure(
        "/creds/account1.json", "claude-sonnet-4-5", ErrorType.RECOVERABLE, 429, "USER_REQUEST_RATE_EXCEEDED"
    )

    payload = manager.request_rate_series(60, 15)
    first = _series_for(payload, "/creds/account0.json")
    second = _series_for(payload, "/creds/account1.json")

    assert (sum(first["success"]), sum(first["rateLimited"])) == (1, 0)
    assert (sum(second["success"]), sum(second["rateLimited"])) == (0, 1)


@pytest.mark.asyncio
async def test_every_account_appears_even_without_traffic(manager):
    payload = manager.request_rate_series(60, 15)

    assert len(payload["accounts"]) == 2
    for entry in payload["accounts"]:
        assert sum(entry["success"]) == 0


def test_bucket_layout_covers_the_window(manager):
    payload = manager.request_rate_series(900, 15)

    assert payload["bucketSeconds"] == 15
    assert len(payload["bucketStarts"]) == 60
    for entry in payload["accounts"]:
        assert len(entry["success"]) == 60
        assert len(entry["rateLimited"]) == 60
        assert len(entry["failure"]) == 60


def test_buckets_are_aligned_to_absolute_time(manager):
    payload = manager.request_rate_series(300, 15)

    starts = payload["bucketStarts"]
    assert all(start % 15 == 0 for start in starts)
    assert starts == sorted(starts)
    assert [starts[index + 1] - starts[index] for index in range(len(starts) - 1)] == [15] * (len(starts) - 1)


@pytest.mark.asyncio
async def test_events_older_than_the_window_are_dropped(manager):
    await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")
    manager._rate_observations[0].at = time.time() - 3600

    series = _series_for(manager.request_rate_series(60, 15), "/creds/account0.json")

    assert sum(series["success"]) == 0


@pytest.mark.asyncio
async def test_history_is_bounded_by_the_estimate_window(manager):
    for _ in range(20):
        await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")
    for observation in manager._rate_observations[:15]:
        observation.at -= RATE_ESTIMATE_WINDOW_SECONDS + 1

    manager.request_rate_series(900, 5)

    assert len(manager._rate_observations) == 5


@pytest.mark.asyncio
async def test_events_for_an_unknown_account_are_ignored(manager):
    await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")
    manager._rate_observations[0].account_id = "/creds/removed.json"

    payload = manager.request_rate_series(60, 15)

    assert all(sum(entry["success"]) == 0 for entry in payload["accounts"])


@pytest.mark.asyncio
async def test_credential_paths_are_never_exposed(manager):
    await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")

    payload = manager.request_rate_series(60, 15)

    assert all("/creds/" not in entry["account"] for entry in payload["accounts"])


@pytest.mark.asyncio
async def test_peak_rpm_reflects_a_burst_not_the_bucket_average(manager):
    now = time.time()
    for index in range(40):
        manager._rate_observations.append(
            RateObservation(
                at=now - 300 + index * 0.05,
                account_id="/creds/account0.json",
                rpm=index + 1,
                rejected=False,
            )
        )

    series = _series_for(manager.request_rate_series(900, 5), "/creds/account0.json")

    # 40 requests inside one second: a bucket total would report far less load.
    assert max(series["peakRpm"]) == 40


@pytest.mark.asyncio
async def test_evenly_spread_traffic_reports_its_true_rate(manager):
    now = time.time()
    for index in range(30):
        manager._rate_observations.append(
            RateObservation(
                at=now - 300 + index * 2,
                account_id="/creds/account0.json",
                rpm=index + 1,
                rejected=False,
            )
        )

    series = _series_for(manager.request_rate_series(900, 5), "/creds/account0.json")

    # 30 requests at one per 2s: never more than 30 inside any 60s window.
    assert max(series["peakRpm"]) == 30


@pytest.mark.asyncio
async def test_guide_sits_at_or_above_cleanly_served_traffic(manager):
    for _ in range(30):
        await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")
    await manager.report_failure(
        "/creds/account0.json", "claude-sonnet-4-5", ErrorType.RECOVERABLE, 429, "USER_REQUEST_RATE_EXCEEDED"
    )

    estimate = manager.estimate_rate_limit("/creds/account0.json")

    assert estimate["safeRpm"] == 30
    assert estimate["limitRpm"] == 31
    assert estimate["limitRpm"] >= estimate["safeRpm"]
    assert estimate["limitUnknownReason"] is None


@pytest.mark.asyncio
async def test_a_rejection_below_served_traffic_does_not_define_the_guide(manager):
    """A 429 at 1/min while 14/min succeeds is not a rate ceiling.

    Observed live: taking the lowest rejection unconditionally pinned the guide
    to the bottom of the chart, so the traffic area always covered it and the
    line stopped being a warning.
    """
    await manager.report_failure(
        "/creds/account0.json", "claude-sonnet-4-5", ErrorType.RECOVERABLE, 429, "USER_REQUEST_RATE_EXCEEDED"
    )
    manager._rate_observations[-1].at = time.time() - 600
    for _ in range(14):
        await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")

    estimate = manager.estimate_rate_limit("/creds/account0.json")

    assert estimate["safeRpm"] == 14
    assert estimate["limitRpm"] is None
    assert estimate["rateLimitSamples"] == 1
    assert estimate["informativeSamples"] == 0
    assert estimate["limitUnknownReason"] == "rejections seen only below the rate this account serves cleanly"


@pytest.mark.asyncio
async def test_the_tightest_informative_rejection_wins(manager):
    now = time.time()
    manager.load_rate_observations(
        [
            ("/creds/account0.json", now - 500, 18, 0, "success"),
            ("/creds/account0.json", now - 400, 40, 1, "rate_limited"),
            # A lower rejection still above proven-safe traffic: the tighter bound.
            ("/creds/account0.json", now - 300, 25, 1, "rate_limited"),
        ]
    )

    estimate = manager.estimate_rate_limit("/creds/account0.json")

    assert estimate["safeRpm"] == 18
    assert estimate["rateLimitSamples"] == 2
    assert estimate["informativeSamples"] == 2
    assert estimate["limitRpm"] == 25
    assert estimate["limitPrecisionRpm"] == 7


@pytest.mark.asyncio
async def test_guide_is_absent_until_a_rejection_is_observed(manager):
    await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")

    estimate = manager.estimate_rate_limit("/creds/account0.json")

    assert estimate["limitRpm"] is None
    assert estimate["rateLimitSamples"] == 0
    assert estimate["limitUnknownReason"] == "no rate rejection observed yet"


@pytest.mark.asyncio
async def test_precision_reports_the_remaining_uncertainty(manager):
    for _ in range(10):
        await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")
    await manager.report_failure(
        "/creds/account0.json", "claude-sonnet-4-5", ErrorType.RECOVERABLE, 429, "USER_REQUEST_RATE_EXCEEDED"
    )

    estimate = manager.estimate_rate_limit("/creds/account0.json")

    assert estimate["limitPrecisionRpm"] == estimate["limitRpm"] - estimate["safeRpm"]
    assert estimate["limitPrecisionRpm"] == 1


@pytest.mark.asyncio
async def test_precision_is_absent_without_a_limit(manager):
    await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")

    assert manager.estimate_rate_limit("/creds/account0.json")["limitPrecisionRpm"] is None


@pytest.mark.asyncio
async def test_a_stale_bound_ages_out_so_a_raised_limit_can_recover(manager):
    """The bound never rises on its own, so old samples must expire.

    Without ageing, an upstream limit that was raised would keep the dashboard
    pinned to the old, lower value indefinitely.
    """
    for _ in range(5):
        await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")
    await manager.report_failure(
        "/creds/account0.json", "claude-sonnet-4-5", ErrorType.RECOVERABLE, 429, "USER_REQUEST_RATE_EXCEEDED"
    )
    assert manager.estimate_rate_limit("/creds/account0.json")["limitRpm"] == 6

    for observation in manager._rate_observations:
        observation.at -= RATE_ESTIMATE_WINDOW_SECONDS + 1

    estimate = manager.estimate_rate_limit("/creds/account0.json")

    assert estimate["limitRpm"] is None
    assert estimate["rateLimitSamples"] == 0


@pytest.mark.asyncio
async def test_estimates_do_not_leak_between_accounts(manager):
    for _ in range(8):
        await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")
    await manager.report_failure(
        "/creds/account0.json", "claude-sonnet-4-5", ErrorType.RECOVERABLE, 429, "USER_REQUEST_RATE_EXCEEDED"
    )

    assert manager.estimate_rate_limit("/creds/account0.json")["limitRpm"] == 9
    assert manager.estimate_rate_limit("/creds/account1.json")["limitRpm"] is None


@pytest.mark.asyncio
async def test_observations_survive_a_restart(manager, tmp_path):
    """The estimate must outlive the process or every deploy resets the guide."""
    for _ in range(12):
        await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")
    await manager.report_failure(
        "/creds/account0.json", "claude-sonnet-4-5", ErrorType.RECOVERABLE, 429, "USER_REQUEST_RATE_EXCEEDED"
    )
    before = manager.estimate_rate_limit("/creds/account0.json")

    rows = manager.drain_unsaved_rate_observations()
    assert rows
    assert manager.drain_unsaved_rate_observations() == []

    restarted = AccountManager(
        credentials_file=str(tmp_path / "credentials.json"),
        state_file=str(tmp_path / "state.json"),
    )
    restarted._accounts["/creds/account0.json"] = Account(id="/creds/account0.json")
    assert restarted.estimate_rate_limit("/creds/account0.json")["limitRpm"] is None

    restarted.load_rate_observations(rows)
    after = restarted.estimate_rate_limit("/creds/account0.json")

    assert after["limitRpm"] == before["limitRpm"]
    assert after["safeRpm"] == before["safeRpm"]
    assert after["limitPrecisionRpm"] == before["limitPrecisionRpm"]


def test_rate_window_is_reported_so_callers_can_label_the_unit(manager):
    payload = manager.request_rate_series(900, 5)

    assert payload["rateWindowSeconds"] == RATE_WINDOW_SECONDS


@pytest.mark.asyncio
async def test_chart_history_survives_a_restart(manager, tmp_path):
    """The chart read a memory-only ring, so every deploy blanked it."""
    for _ in range(6):
        await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")
    await manager.report_failure(
        "/creds/account0.json", "claude-sonnet-4-5", ErrorType.RECOVERABLE, 429, "USER_REQUEST_RATE_EXCEEDED"
    )
    before = _series_for(manager.request_rate_series(900, 5), "/creds/account0.json")
    rows = manager.drain_unsaved_rate_observations()

    restarted = AccountManager(
        credentials_file=str(tmp_path / "credentials.json"),
        state_file=str(tmp_path / "state.json"),
    )
    restarted._accounts["/creds/account0.json"] = Account(id="/creds/account0.json")
    blank = _series_for(restarted.request_rate_series(900, 5), "/creds/account0.json")
    assert sum(blank["success"]) == 0

    restarted.load_rate_observations(rows)
    after = _series_for(restarted.request_rate_series(900, 5), "/creds/account0.json")

    assert sum(after["success"]) == sum(before["success"]) == 6
    assert sum(after["rateLimited"]) == sum(before["rateLimited"]) == 1
    assert max(after["peakRpm"]) == max(before["peakRpm"])


@pytest.mark.asyncio
async def test_rate_is_correct_on_the_first_request_after_a_restart(manager, tmp_path):
    """Rate was counted from the memory ring, so a fresh process reported 1/min
    however hard the account was actually being driven."""
    for _ in range(10):
        await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")
    rows = manager.drain_unsaved_rate_observations()

    restarted = AccountManager(
        credentials_file=str(tmp_path / "credentials.json"),
        state_file=str(tmp_path / "state.json"),
    )
    restarted._accounts["/creds/account0.json"] = Account(id="/creds/account0.json")
    restarted.load_rate_observations(rows)

    await restarted.report_success("/creds/account0.json", "claude-sonnet-4-5")

    assert restarted._rate_observations[-1].rpm == 11


@pytest.mark.asyncio
async def test_non_rate_failures_are_still_charted(manager):
    await manager.report_failure(
        "/creds/account0.json", "claude-sonnet-4-5", ErrorType.RECOVERABLE, 402, "MONTHLY_REQUEST_COUNT"
    )

    series = _series_for(manager.request_rate_series(900, 5), "/creds/account0.json")

    assert sum(series["failure"]) == 1
    assert sum(series["rateLimited"]) == 0


class TestRoutingStateOnTheSeries:
    """The series carries why an account is (not) a routing target.

    The dashboard hides unroutable accounts from the rate chart, so this field is
    load-bearing rather than decorative: without it the client would have to join
    two endpoints and guess, and the accounts endpoint reports the pool as it is
    now while rate observations cover a window.
    """

    @pytest.mark.asyncio
    async def test_a_healthy_account_reports_available(self, manager):
        await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")
        # An account with no auth_manager classifies as "uninitialized" before any
        # quota check is reached, so initialize the ones under test.
        manager._accounts["/creds/account0.json"].auth_manager = object()

        series = _series_for(manager.request_rate_series(60, 15), "/creds/account0.json")

        assert series["routingState"] == "available"

    @pytest.mark.asyncio
    async def test_a_suspended_account_reports_suspended(self, manager):
        await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")
        manager._accounts["/creds/account0.json"].suspended_until = time.time() + 3600

        series = _series_for(manager.request_rate_series(60, 15), "/creds/account0.json")

        assert series["routingState"] == "suspended"

    @pytest.mark.asyncio
    async def test_a_quota_exhausted_account_reports_it(self, manager):
        await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")
        manager._accounts["/creds/account0.json"].quota_exhausted_until = time.time() + 3600

        series = _series_for(manager.request_rate_series(60, 15), "/creds/account0.json")

        assert series["routingState"] == "quota_exhausted"

    @pytest.mark.asyncio
    async def test_a_spent_account_reports_depleted(self, manager):
        # Usage telemetry says the allowance is gone and overage is off. Excluded
        # from routing, so the chart hides it, but by inference rather than an
        # upstream verdict.
        await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")
        account = manager._accounts["/creds/account0.json"]
        account.auth_manager = object()
        account.quota_headroom = 0.0
        account.quota_overage_enabled = False

        series = _series_for(manager.request_rate_series(60, 15), "/creds/account0.json")

        assert series["routingState"] == "quota_depleted"

    @pytest.mark.asyncio
    async def test_a_deregistered_account_leaves_the_series_entirely(self, manager):
        """Series are seeded from the live pool, so removing an account drops its history.

        Pinned because the dashboard's hide rule treats a null state as "keep
        charting". That branch is unreachable today, and this test is what would
        fail first if `_observations_by_account` ever started emitting orphaned
        observations - at which point the frontend contract needs revisiting
        rather than silently charting an account that no longer exists.
        """
        await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")
        del manager._accounts["/creds/account0.json"]

        payload = manager.request_rate_series(60, 15)

        labels = [entry["account"] for entry in payload["accounts"]]
        assert account_label("/creds/account0.json") not in labels

    @pytest.mark.asyncio
    async def test_every_series_carries_the_field(self, manager):
        """No consumer has to treat the key as optional."""
        await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")
        await manager.report_success("/creds/account1.json", "claude-sonnet-4-5")

        payload = manager.request_rate_series(60, 15)

        assert payload["accounts"]
        for entry in payload["accounts"]:
            assert "routingState" in entry
