"""Quota priming for freshly registered accounts.

Both registration routes (manual credential entry and browser device login)
must poll usage before answering. The periodic refresh runs on a
USAGE_REFRESH_INTERVAL_SECONDS cadence, so an account registered between two
ticks otherwise sits in the dashboard with no email, tier, or usage until the
next one. The internal account key that `register_account` hands back for that
lookup is a credential path, so it must not survive into the response.
"""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro.account_manager import Account

_USAGE = {
    "email": "fresh@example.test",
    "subscriptionTitle": "KIRO FREE",
    "subscriptionType": "Q_DEVELOPER_STANDALONE_FREE",
    "resourceType": "CREDIT",
    "currentUsage": 0.0,
    "usageLimit": 50.0,
    "usagePercent": 0.0,
    "unit": "INVOCATIONS",
    "overageStatus": "DISABLED",
    "overageUsed": 0.0,
    "overageRate": None,
    "nextDateReset": 1788220800.0,
    "daysUntilReset": 3,
}

_ACCOUNT_KEY = "/data/logins/builder-id-fresh.json"


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    module = importlib.reload(importlib.import_module("kiro.dashboard"))
    module.initialize_dashboard_store()
    return module


def _manager_with_registered_account() -> SimpleNamespace:
    account = Account(id=_ACCOUNT_KEY)
    account.auth_manager = MagicMock()
    return SimpleNamespace(_accounts={_ACCOUNT_KEY: account})


def _registration_result() -> dict:
    return {
        "accountId": "472bf78ae092",
        "accountKey": _ACCOUNT_KEY,
        "type": "json",
        "initialized": True,
    }


def _prime(dashboard, manager, result):
    with patch.object(dashboard, "fetch_account_usage", AsyncMock(return_value=_USAGE)):
        return asyncio.run(dashboard.prime_registered_account_usage(manager, result))


def test_registration_stores_usage_for_the_new_account(dashboard):
    manager = _manager_with_registered_account()

    _prime(dashboard, manager, _registration_result())

    view = dashboard._account_view(manager._accounts[_ACCOUNT_KEY])
    assert view["usage"]["email"] == "fresh@example.test"
    assert view["usage"]["subscriptionTitle"] == "KIRO FREE"
    assert view["usage"]["usageLimit"] == 50.0


def test_internal_account_key_is_not_returned(dashboard):
    result = _prime(dashboard, _manager_with_registered_account(), _registration_result())

    assert "accountKey" not in result
    assert _ACCOUNT_KEY not in str(result)
    assert result["accountId"] == "472bf78ae092"


def test_uninitialized_account_is_not_polled(dashboard):
    manager = _manager_with_registered_account()
    manager._accounts[_ACCOUNT_KEY].auth_manager = None

    with patch.object(dashboard, "fetch_account_usage", AsyncMock(return_value=_USAGE)) as fetch:
        asyncio.run(dashboard.prime_registered_account_usage(manager, _registration_result()))

    fetch.assert_not_awaited()
    assert dashboard._account_view(manager._accounts[_ACCOUNT_KEY])["usage"] is None


def test_a_failed_poll_does_not_fail_the_registration(dashboard):
    manager = _manager_with_registered_account()

    with patch.object(dashboard, "fetch_account_usage", AsyncMock(side_effect=RuntimeError("upstream 403"))):
        result = asyncio.run(dashboard.prime_registered_account_usage(manager, _registration_result()))

    assert result["initialized"] is True
    assert dashboard._account_view(manager._accounts[_ACCOUNT_KEY])["usage"]["error"] == "upstream 403"


def test_both_registration_routes_prime_usage():
    """Neither route may answer without priming; device login used to skip it."""
    import inspect

    import kiro.dashboard as module

    for route in (module.dashboard_register_account, module.dashboard_register_device_login):
        assert "prime_registered_account_usage" in inspect.getsource(route)
