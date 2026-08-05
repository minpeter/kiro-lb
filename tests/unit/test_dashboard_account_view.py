"""Contract tests for the dashboard account view.

The failure counter alone no longer describes availability: a rate limit and a
quota exhaustion both bypass the Circuit Breaker on purpose, so an excluded
account can sit at `failures == 0`. The view must expose the routing state the
router actually uses, otherwise the dashboard shows a quota-dead account as
ready.
"""

import importlib
import time
from unittest.mock import MagicMock

import pytest

from kiro.account_manager import Account, account_routing_state
from kiro.config import ACCOUNT_RECOVERY_TIMEOUT


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    module = importlib.reload(importlib.import_module("kiro.dashboard"))
    module.initialize_dashboard_store()
    return module


def _initialized_account() -> Account:
    account = Account(id="/creds/account.json")
    account.auth_manager = MagicMock()
    return account


def test_available_account_is_reported_as_routing_target(dashboard):
    view = dashboard._account_view(_initialized_account())

    assert view["routingState"] == "available"
    assert view["eligibleInSeconds"] == 0


def test_quota_exhausted_account_is_not_reported_as_available(dashboard):
    account = _initialized_account()
    account.quota_exhausted_until = time.time() + 3600

    view = dashboard._account_view(account)

    assert view["routingState"] == "quota_exhausted"
    assert 3500 < view["eligibleInSeconds"] <= 3600
    assert view["failures"] == 0


def test_rate_limited_account_is_distinguished_from_quota(dashboard):
    account = _initialized_account()
    account.rate_limited_until = time.time() + 10

    view = dashboard._account_view(account)

    assert view["routingState"] == "rate_limited"
    assert 0 < view["eligibleInSeconds"] <= 10


def test_cooling_down_account_reports_remaining_backoff(dashboard):
    account = _initialized_account()
    account.failures = 3
    account.last_failure_time = time.time()

    view = dashboard._account_view(account)

    third_failure_backoff = ACCOUNT_RECOVERY_TIMEOUT * 4
    assert view["routingState"] == "cooling_down"
    assert view["eligibleInSeconds"] == pytest.approx(third_failure_backoff, abs=2)


def test_uninitialized_account_is_pending(dashboard):
    view = dashboard._account_view(Account(id="/creds/fresh.json"))

    assert view["routingState"] == "uninitialized"
    assert view["initialized"] is False


def test_credential_path_is_never_exposed(dashboard):
    view = dashboard._account_view(_initialized_account())

    assert "/creds/" not in view["id"]
    assert len(view["id"]) == 12


def test_longer_quota_exclusion_outranks_shorter_cooldown():
    account = _initialized_account()
    account.failures = 1
    account.last_failure_time = time.time()
    account.quota_exhausted_until = time.time() + 3600

    state, _ = account_routing_state(account)

    assert state == "quota_exhausted"


def test_view_exposes_routing_weight_input(dashboard):
    account = _initialized_account()
    account.quota_headroom = 0.42

    view = dashboard._account_view(account)

    assert view["quotaHeadroom"] == 0.42


def test_view_reports_unknown_routing_weight_as_null(dashboard):
    # The persisted `usage` row can look fresh while the router holds no weight
    # (failed poll, or a restart before the first refresh). Reporting null here
    # is what lets an operator tell those apart.
    view = dashboard._account_view(_initialized_account())

    assert view["quotaHeadroom"] is None


def test_view_exposes_the_quota_period(dashboard):
    account = _initialized_account()
    account.quota_resets_at = 1785542400.0
    account.quota_overage_enabled = False

    view = dashboard._account_view(account)

    assert view["quotaResetsAt"] == 1785542400.0
    assert view["quotaOverageEnabled"] is False


def test_view_reports_unknown_quota_period_as_null(dashboard):
    # 0.0 means "no reset date known", which is a different fact from a reset at
    # the epoch; the client renders the two differently.
    view = dashboard._account_view(_initialized_account())

    assert view["quotaResetsAt"] is None
    assert view["quotaOverageEnabled"] is None


def test_spent_account_is_not_reported_as_available(dashboard):
    account = _initialized_account()
    account.quota_headroom = 0.0
    account.quota_overage_enabled = False
    account.quota_resets_at = time.time() + 86400

    view = dashboard._account_view(account)

    assert view["routingState"] == "quota_depleted"
    assert view["eligibleInSeconds"] > 0
