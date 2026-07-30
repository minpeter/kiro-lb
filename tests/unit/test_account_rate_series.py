"""Contract tests for the per-account request-rate series.

The dashboard request log records the client-facing result, so a 429 that
failover recovered from is filed as a 200 and carries no account attribution.
This series is the only place a rate-limit burst is visible per account.
"""

import time
from typing import Any

import pytest

from kiro.account_errors import ErrorType
from kiro.account_manager import Account, AccountManager, account_label
from kiro.config import ROUTING_EVENT_HISTORY


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
