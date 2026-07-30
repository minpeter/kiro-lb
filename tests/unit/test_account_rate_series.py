"""Contract tests for the per-account request-rate series.

The dashboard request log records the client-facing result, so a 429 that
failover recovered from is filed as a 200 and carries no account attribution.
This series is the only place a rate-limit burst is visible per account.
"""

import time
from typing import Any

import pytest

from kiro.account_errors import ErrorType
from kiro.account_manager import Account, AccountManager, RoutingEvent, account_label
from kiro.config import RATE_WINDOW_SECONDS, ROUTING_EVENT_HISTORY


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
    manager._routing_events[0].at = time.time() - 3600

    series = _series_for(manager.request_rate_series(60, 15), "/creds/account0.json")

    assert sum(series["success"]) == 0


@pytest.mark.asyncio
async def test_history_is_bounded(manager):
    for _ in range(ROUTING_EVENT_HISTORY + 50):
        await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")

    assert len(manager._routing_events) == ROUTING_EVENT_HISTORY


@pytest.mark.asyncio
async def test_events_for_an_unknown_account_are_ignored(manager):
    await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")
    manager._routing_events[0].account_id = "/creds/removed.json"

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
    burst = [(now - 300 + index * 0.05, "success") for index in range(40)]
    for at, outcome in burst:
        manager._routing_events.append(RoutingEvent(at=at, account_id="/creds/account0.json", outcome=outcome))

    series = _series_for(manager.request_rate_series(900, 5), "/creds/account0.json")

    # 40 requests inside one second: a bucket total would report far less load.
    assert max(series["peakRpm"]) == 40


@pytest.mark.asyncio
async def test_evenly_spread_traffic_reports_its_true_rate(manager):
    now = time.time()
    for index in range(30):
        manager._routing_events.append(
            RoutingEvent(at=now - 300 + index * 2, account_id="/creds/account0.json", outcome="success")
        )

    series = _series_for(manager.request_rate_series(900, 5), "/creds/account0.json")

    # 30 requests at one per 2s: never more than 30 inside any 60s window.
    assert max(series["peakRpm"]) == 30


@pytest.mark.asyncio
async def test_ceiling_is_the_lowest_rate_that_drew_a_rejection(manager):
    now = time.time()
    events = [(now - 600 + index * 0.05, "success") for index in range(50)]
    events.append((now - 599, "rate_limited"))
    # A later, smaller burst also gets rejected: it is the tighter upper bound.
    events += [(now - 300 + index * 0.05, "success") for index in range(20)]
    events.append((now - 299, "rate_limited"))
    for at, outcome in sorted(events):
        manager._routing_events.append(RoutingEvent(at=at, account_id="/creds/account0.json", outcome=outcome))

    series = _series_for(manager.request_rate_series(900, 5), "/creds/account0.json")

    assert series["rateLimitSamples"] == 2
    assert series["ceilingRpm"] == 21
    assert series["ceilingRpm"] < max(series["peakRpm"])


@pytest.mark.asyncio
async def test_ceiling_is_absent_until_a_rejection_is_observed(manager):
    await manager.report_success("/creds/account0.json", "claude-sonnet-4-5")

    series = _series_for(manager.request_rate_series(900, 5), "/creds/account0.json")

    assert series["ceilingRpm"] is None
    assert series["rateLimitSamples"] == 0


@pytest.mark.asyncio
async def test_served_peak_brackets_the_limit_from_below(manager):
    now = time.time()
    events = [(now - 600 + index * 0.05, "success") for index in range(15)]
    events += [(now - 300 + index * 0.05, "success") for index in range(25)]
    # The rejection arrives after the burst, so 25 is the most served cleanly.
    events.append((now - 300 + 25 * 0.05, "rate_limited"))
    for at, outcome in sorted(events):
        manager._routing_events.append(RoutingEvent(at=at, account_id="/creds/account0.json", outcome=outcome))

    series = _series_for(manager.request_rate_series(900, 5), "/creds/account0.json")

    assert series["servedPeakRpm"] == 25
    assert series["ceilingRpm"] == 26
    assert series["servedPeakRpm"] < series["ceilingRpm"]


def test_rate_window_is_reported_so_callers_can_label_the_unit(manager):
    payload = manager.request_rate_series(900, 5)

    assert payload["rateWindowSeconds"] == RATE_WINDOW_SECONDS
