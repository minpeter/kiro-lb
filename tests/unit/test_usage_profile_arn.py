# -*- coding: utf-8 -*-
"""The usage query needs a profile ARN, and the manager may not hold one yet.

get_access_token returns a cached token without reading the store, so a manager
that never had to refresh can have a valid token and an empty ARN. The upstream
answers that with a bare "Invalid profileArn", which reads on the dashboard as a
broken account rather than a missing field.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro import store, usage


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    store.initialize()
    yield


def _persist_arn(account_id: str, arn: str) -> None:
    """save_internal_credential needs a registered row, so seed one directly."""
    with store.connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO account_sources(account_id, position, config_json, credential_json) "
            "VALUES (?, 0, ?, ?)",
            (account_id, json.dumps({"type": "internal"}), json.dumps({"profileArn": arn})),
        )


def _account(arn_in_manager: str | None, account_id: str = "acct-1"):
    auth = SimpleNamespace(
        _profile_arn=arn_in_manager,
        _internal_account_id=account_id,
        request_profile_arn=arn_in_manager,
        profile_arn=arn_in_manager,
        api_region="us-east-1",
        region="us-east-1",
        api_host="https://runtime.us-east-1.kiro.dev",
    )
    return SimpleNamespace(id=account_id, auth_manager=auth)


class TestStoredProfileArn:
    def test_reads_the_persisted_arn(self):
        _persist_arn("acct-1", "arn:aws:codewhisperer:x")
        assert usage._stored_profile_arn(_account(None)) == "arn:aws:codewhisperer:x"

    def test_returns_none_without_a_document(self):
        assert usage._stored_profile_arn(_account(None)) is None

    def test_ignores_a_blank_arn(self):
        _persist_arn("acct-1", "   ")
        assert usage._stored_profile_arn(_account(None)) is None

    def test_survives_an_unreadable_store(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("no store")

        monkeypatch.setattr(store, "load_internal_credential", boom)
        assert usage._stored_profile_arn(_account(None)) is None


@pytest.mark.asyncio
class TestUsageQueryRequiresArn:
    async def test_refuses_to_query_without_an_arn(self, monkeypatch):
        """Better a named failure than a 400 the operator has to decode."""
        account = _account(None)

        async def token():
            return "t"

        account.auth_manager.get_access_token = token
        monkeypatch.setattr(usage, "get_kiro_headers", lambda *_: {})

        with pytest.raises(RuntimeError, match="profile ARN is not available"):
            await usage.fetch_account_usage(account)

    async def test_recovers_the_arn_from_the_store(self, monkeypatch):
        _persist_arn("acct-1", "arn:recovered")
        account = _account(None)

        async def token():
            return "t"

        account.auth_manager.get_access_token = token
        monkeypatch.setattr(usage, "get_kiro_headers", lambda *_: {})

        sent: dict = {}

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"usageBreakdownList": [], "subscriptionInfo": {}, "userInfo": {}}

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def post(self, url, params=None, json=None, headers=None):
                sent["params"] = params
                sent["body"] = json
                return FakeResponse()

        monkeypatch.setattr(usage.httpx, "AsyncClient", lambda **_: FakeClient())

        await usage.fetch_account_usage(account)
        assert sent["params"]["profileArn"] == "arn:recovered"
        assert sent["body"]["profileArn"] == "arn:recovered", "the upstream rejects a body without it"
