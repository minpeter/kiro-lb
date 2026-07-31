"""Account email exposure on the dashboard accounts view.

Account IDs are hashed credential paths, so a row alone cannot tell an operator
which Kiro account it is. The usage poll already asks for the identity block
(`isEmailRequired`), and these tests pin that the email survives normalization,
the additive store column, and the account view - while the upstream user ID
never does.
"""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro.account_manager import Account
from kiro.usage import fetch_account_usage

_UPSTREAM_PAYLOAD = {
    "subscriptionInfo": {"subscriptionTitle": "KIRO POWER", "type": "Q_DEVELOPER_STANDALONE_POWER"},
    "overageConfiguration": {"overageStatus": "DISABLED"},
    "usageBreakdownList": [
        {
            "resourceType": "CREDIT",
            "currentUsageWithPrecision": 4663.3,
            "usageLimitWithPrecision": 10000.0,
            "currentOveragesWithPrecision": 0.0,
            "unit": "INVOCATIONS",
        }
    ],
    "nextDateReset": 1785542400.0,
    "daysUntilReset": 3,
    "userInfo": {"email": "pool-account@example.test", "userId": "d-9067642ac7.54c824b8-3021-701b"},
}


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    module = importlib.reload(importlib.import_module("kiro.dashboard"))
    module.initialize_dashboard_store()
    return module


def _account_with_auth() -> Account:
    account = Account(id="/creds/account.json")
    account.auth_manager = SimpleNamespace(
        get_access_token=AsyncMock(return_value="mock-token"),
        profile_arn="arn:aws:codewhisperer:us-east-1:123456789012:profile/example",
        api_host="https://runtime.us-east-1.kiro.dev",
        region="us-east-1",
        fingerprint="mock-fingerprint",
    )
    return account


def _fetch(payload: dict) -> dict:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = payload
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with patch("kiro.usage.httpx.AsyncClient", return_value=client):
        return asyncio.run(fetch_account_usage(_account_with_auth()))


def test_upstream_email_is_normalized_into_the_usage_summary():
    assert _fetch(_UPSTREAM_PAYLOAD)["email"] == "pool-account@example.test"


def test_upstream_user_id_is_not_exposed():
    usage = _fetch(_UPSTREAM_PAYLOAD)

    assert "userId" not in usage
    assert "d-9067642ac7.54c824b8-3021-701b" not in str(usage)


def test_missing_identity_block_yields_no_email():
    payload = {key: value for key, value in _UPSTREAM_PAYLOAD.items() if key != "userInfo"}

    assert _fetch(payload)["email"] is None


def test_blank_upstream_email_is_stored_as_absent():
    payload = {**_UPSTREAM_PAYLOAD, "userInfo": {"email": "", "userId": "d-1"}}

    assert _fetch(payload)["email"] is None


def test_refreshed_email_reaches_the_account_view(dashboard):
    account = _account_with_auth()
    with patch.object(dashboard, "fetch_account_usage", AsyncMock(return_value=_fetch(_UPSTREAM_PAYLOAD))):
        asyncio.run(dashboard.refresh_account_usage(account))

    view = dashboard._account_view(account)

    assert view["usage"]["email"] == "pool-account@example.test"
    assert view["usage"]["subscriptionTitle"] == "KIRO POWER"


def test_email_column_is_added_to_a_pre_existing_store(dashboard):
    with dashboard._db() as conn:
        conn.execute("DROP TABLE account_usage")
        conn.execute(
            """CREATE TABLE account_usage (
                account_id TEXT PRIMARY KEY,
                subscription_title TEXT,
                updated_at INTEGER NOT NULL,
                error TEXT
            )"""
        )
        conn.execute("INSERT INTO account_usage(account_id, updated_at) VALUES ('/creds/legacy.json', 1)")

    dashboard.initialize_dashboard_store()

    with dashboard._db() as conn:
        assert "email" in {row["name"] for row in conn.execute("PRAGMA table_info(account_usage)")}
        row = conn.execute("SELECT account_id, email FROM account_usage").fetchone()
        assert (row["account_id"], row["email"]) == ("/creds/legacy.json", None)


def test_account_without_a_usage_poll_reports_no_usage(dashboard):
    view = dashboard._account_view(_account_with_auth())

    assert view["usage"] is None
