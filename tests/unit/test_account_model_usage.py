# -*- coding: utf-8 -*-
"""Token accounting at the (key, account, model) grain.

The store previously held tokens per key and request counts per account, so
"tokens per account per model" had no answer and could not be backfilled: the
request log carries no account id. These tests pin the grain, the reconciliation
between the two views, and the failover attribution rule.
"""

from __future__ import annotations

import importlib

import pytest

from kiro.usage_tracking import (
    UNKNOWN_ACCOUNT_ID,
    current_account_id,
    current_api_key_id,
    drain_pending_usage,
    record_token_usage,
    restore_pending_usage,
)


@pytest.fixture(autouse=True)
def _clean_pending():
    drain_pending_usage()
    yield
    drain_pending_usage()


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    """A dashboard module bound to an empty store."""
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    import kiro.store as store

    importlib.reload(store)
    import kiro.dashboard as dashboard_module

    importlib.reload(dashboard_module)
    dashboard_module.initialize_dashboard_store()
    return dashboard_module


def _record(key_id: str, account_id: str | None, model: str, prompt: int, completion: int, seconds=None):
    current_api_key_id.set(key_id)
    current_account_id.set(account_id)
    record_token_usage(model, prompt, completion, seconds)


class TestRecordingGrain:
    def test_tokens_are_attributed_to_key_account_and_model(self):
        _record("key_a", "/creds/one.json", "claude-sonnet-4-5", 10, 5)

        assert drain_pending_usage() == [("key_a", "/creds/one.json", "claude-sonnet-4.5", 10, 5, 1, 0, 0)]

    def test_same_key_on_two_accounts_stays_separate(self):
        # The whole point of the new axis: one key's traffic split across accounts
        # must not collapse into a single row.
        _record("key_a", "/creds/one.json", "claude-sonnet-4-5", 10, 5)
        _record("key_a", "/creds/two.json", "claude-sonnet-4-5", 20, 7)

        drained = {(row[0], row[1]): row[3:6] for row in drain_pending_usage()}
        assert drained[("key_a", "/creds/one.json")] == (10, 5, 1)
        assert drained[("key_a", "/creds/two.json")] == (20, 7, 1)

    def test_same_account_on_two_models_stays_separate(self):
        _record("key_a", "/creds/one.json", "claude-sonnet-4-5", 10, 5)
        _record("key_a", "/creds/one.json", "claude-haiku-4-5", 1, 2)

        drained = {row[2]: row[3:6] for row in drain_pending_usage()}
        assert drained["claude-sonnet-4.5"] == (10, 5, 1)
        assert drained["claude-haiku-4.5"] == (1, 2, 1)

    def test_repeated_requests_accumulate_in_one_row(self):
        for _ in range(3):
            _record("key_a", "/creds/one.json", "claude-sonnet-4-5", 10, 5)

        assert drain_pending_usage() == [("key_a", "/creds/one.json", "claude-sonnet-4.5", 30, 15, 3, 0, 0)]

    def test_model_name_is_normalized_before_keying(self):
        # Same rule the per-key table already follows: the dotted and dated forms
        # are one model, and splitting them produces rows nothing can rejoin.
        _record("key_a", "/creds/one.json", "claude-sonnet-4.5", 10, 5)
        _record("key_a", "/creds/one.json", "claude-sonnet-4-5", 10, 5)

        assert len(drain_pending_usage()) == 1

    def test_unattributed_account_is_bucketed_not_dropped(self):
        # Legacy single-account mode records tokens without going through
        # selection. Dropping them would make the per-account totals silently
        # disagree with the per-key ones.
        _record("key_a", None, "claude-sonnet-4-5", 10, 5)

        drained = drain_pending_usage()
        assert [row[1] for row in drained] == [UNKNOWN_ACCOUNT_ID]
        assert drained[0][3:6] == (10, 5, 1)

    def test_missing_key_still_records_nothing(self):
        # Unchanged contract: no key means no attribution at all.
        current_api_key_id.set(None)
        current_account_id.set("/creds/one.json")
        record_token_usage("claude-sonnet-4-5", 10, 5)

        assert drain_pending_usage() == []

    def test_timing_only_counts_timed_requests(self):
        _record("key_a", "/creds/one.json", "claude-sonnet-4-5", 10, 5, 2.0)
        _record("key_a", "/creds/one.json", "claude-sonnet-4-5", 10, 7, None)

        (row,) = drain_pending_usage()
        _, _, _, prompt, completion, requests, generation_ms, timed = row
        assert (prompt, completion, requests) == (20, 12, 2)
        assert generation_ms == 2000
        # Only the timed request's output may divide the duration.
        assert timed == 5

    def test_restore_preserves_the_account_axis(self):
        # A failed durable flush is added back rather than overwritten, and must
        # not lose which account the tokens belonged to.
        _record("key_a", "/creds/one.json", "claude-sonnet-4-5", 10, 5)
        batch = drain_pending_usage()
        _record("key_a", "/creds/one.json", "claude-sonnet-4-5", 1, 1)
        restore_pending_usage(batch)

        assert drain_pending_usage() == [("key_a", "/creds/one.json", "claude-sonnet-4.5", 11, 6, 2, 0, 0)]


class TestFlushAndReconciliation:
    def test_flush_writes_both_grains(self, dashboard):
        _record("key_a", "/creds/one.json", "claude-sonnet-4-5", 10, 5)

        assert dashboard.flush_key_model_usage() == 1

        per_account = dashboard.account_model_usage()
        assert per_account["/creds/one.json"][0]["totalTokens"] == 15
        per_key = dashboard.key_model_usage()
        assert per_key["key_a"][0]["totalTokens"] == 15

    def test_per_key_totals_equal_the_sum_over_accounts(self, dashboard):
        # The invariant that justifies writing both tables from one batch: the
        # older view must stay exactly the sum of the new one.
        _record("key_a", "/creds/one.json", "claude-sonnet-4-5", 10, 5)
        _record("key_a", "/creds/two.json", "claude-sonnet-4-5", 20, 7)
        _record("key_b", "/creds/one.json", "claude-sonnet-4-5", 3, 1)
        dashboard.flush_key_model_usage()

        per_key = dashboard.key_model_usage()
        per_account = dashboard.account_model_usage()
        key_total = sum(entry["totalTokens"] for rows in per_key.values() for entry in rows)
        account_total = sum(entry["totalTokens"] for rows in per_account.values() for entry in rows)
        assert key_total == account_total == 46

    def test_account_view_sums_over_keys(self, dashboard):
        # Two keys hitting one account report as that account's single total: the
        # per-key breakdown is already available from the other view.
        _record("key_a", "/creds/one.json", "claude-sonnet-4-5", 10, 5)
        _record("key_b", "/creds/one.json", "claude-sonnet-4-5", 20, 7)
        dashboard.flush_key_model_usage()

        rows = dashboard.account_model_usage()["/creds/one.json"]
        assert len(rows) == 1
        assert rows[0]["totalTokens"] == 42
        assert rows[0]["requests"] == 2

    def test_repeated_flushes_accumulate(self, dashboard):
        _record("key_a", "/creds/one.json", "claude-sonnet-4-5", 10, 5)
        dashboard.flush_key_model_usage()
        _record("key_a", "/creds/one.json", "claude-sonnet-4-5", 10, 5)
        dashboard.flush_key_model_usage()

        assert dashboard.account_model_usage()["/creds/one.json"][0]["totalTokens"] == 30

    def test_empty_drain_writes_nothing(self, dashboard):
        assert dashboard.flush_key_model_usage() == 0
        assert dashboard.account_model_usage() == {}

    def test_throughput_uses_only_timed_tokens(self, dashboard):
        # Same trap as the per-key view: dividing the full output total by a
        # partial duration reported 82,752 tok/s on the live store.
        _record("key_a", "/creds/one.json", "claude-sonnet-4-5", 10, 100, 2.0)
        _record("key_a", "/creds/one.json", "claude-sonnet-4-5", 10, 900, None)
        dashboard.flush_key_model_usage()

        row = dashboard.account_model_usage()["/creds/one.json"][0]
        assert row["completionTokens"] == 1000
        assert row["tokensPerSecond"] == pytest.approx(50.0)

    def test_throughput_is_absent_rather_than_zero_when_untimed(self, dashboard):
        _record("key_a", "/creds/one.json", "claude-sonnet-4-5", 10, 5, None)
        dashboard.flush_key_model_usage()

        assert dashboard.account_model_usage()["/creds/one.json"][0]["tokensPerSecond"] is None

    def test_a_failed_flush_restores_the_batch(self, dashboard):
        _record("key_a", "/creds/one.json", "claude-sonnet-4-5", 10, 5)

        working_db = dashboard._db

        def boom(*_args, **_kwargs):
            raise RuntimeError("store unavailable")

        # Swapped by hand rather than with monkeypatch.undo(): undo would also
        # revert the fixture's DASHBOARD_DATA_DIR and repoint the store.
        dashboard._db = boom
        try:
            assert dashboard.flush_key_model_usage() == 0
        finally:
            dashboard._db = working_db

        # Nothing was lost: the next successful flush still writes the tokens.
        assert dashboard.flush_key_model_usage() == 1
        assert dashboard.account_model_usage()["/creds/one.json"][0]["totalTokens"] == 15


class TestMigration:
    def test_initialize_is_idempotent(self, dashboard):
        _record("key_a", "/creds/one.json", "claude-sonnet-4-5", 10, 5)
        dashboard.flush_key_model_usage()

        # A restart re-runs initialization; it must not drop accumulated rows.
        dashboard.initialize_dashboard_store()

        assert dashboard.account_model_usage()["/creds/one.json"][0]["totalTokens"] == 15

    def test_table_is_added_to_a_store_that_predates_it(self, tmp_path, monkeypatch):
        # The realistic upgrade path: an existing store with per-key rows and no
        # account grain. Initialization must add the table without touching the
        # old data, and the two views then disagree until new traffic arrives -
        # by design, since the history cannot be backfilled.
        monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
        import kiro.store as store

        importlib.reload(store)
        import kiro.dashboard as dashboard_module

        importlib.reload(dashboard_module)
        dashboard_module.initialize_dashboard_store()
        with dashboard_module._db() as conn:
            conn.execute("DROP TABLE account_model_usage")
            conn.execute(
                "INSERT INTO key_model_usage(key_id, model, prompt_tokens, completion_tokens,"
                " requests, generation_ms, timed_completion_tokens, updated_at)"
                " VALUES ('key_old', 'claude-sonnet-4-5', 100, 50, 5, 0, 0, 1)"
            )

        dashboard_module.initialize_dashboard_store()

        assert dashboard_module.key_model_usage()["key_old"][0]["totalTokens"] == 150
        assert dashboard_module.account_model_usage() == {}


class TestUsageRoute:
    """The /api/dashboard/accounts/usage contract."""

    @pytest.fixture
    def client(self, dashboard, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        monkeypatch.setenv("DASHBOARD_PASSWORD", "dashboard-password")
        app = FastAPI()
        app.include_router(dashboard.router)
        return TestClient(app)

    def _login(self, client):
        assert client.post("/api/dashboard/login", json={"password": "dashboard-password"}).status_code == 200

    def test_requires_authentication(self, client):
        # Usage is operational data about the pool; it must not be readable
        # without a session, like every other dashboard route.
        assert client.get("/api/dashboard/accounts/usage").status_code == 401

    def test_returns_tokens_grouped_by_account(self, client, dashboard):
        self._login(client)
        _record("key_a", "/data/logins/builder-id-7Oay0539aYPtzTFS.json", "claude-sonnet-4-5", 10, 5)

        body = client.get("/api/dashboard/accounts/usage").json()

        from kiro.account_manager import account_label

        label = account_label("/data/logins/builder-id-7Oay0539aYPtzTFS.json")
        assert body["usage"][label]["totalTokens"] == 15
        assert body["usage"][label]["requests"] == 1
        assert body["usage"][label]["models"][0]["model"] == "claude-sonnet-4.5"

    def test_never_exposes_a_credential_path(self, client, dashboard):
        self._login(client)
        path = "/data/logins/builder-id-7Oay0539aYPtzTFS.json"
        _record("key_a", path, "claude-sonnet-4-5", 10, 5)

        assert path not in client.get("/api/dashboard/accounts/usage").text

    def test_flushes_pending_counts_before_answering(self, client, dashboard):
        # Without the flush a request served seconds ago is missing from the
        # answer, which reads as "this account did nothing".
        self._login(client)
        _record("key_a", "/creds/one.json", "claude-sonnet-4-5", 10, 5)

        body = client.get("/api/dashboard/accounts/usage").json()

        assert sum(entry["totalTokens"] for entry in body["usage"].values()) == 15

    def test_labels_accounts_with_their_email_when_known(self, client, dashboard):
        self._login(client)
        path = "/creds/one.json"
        with dashboard._db() as conn:
            conn.execute(
                "INSERT INTO account_usage(account_id, email, updated_at) VALUES (?, ?, 1)",
                (path, "someone@example.com"),
            )
        _record("key_a", path, "claude-sonnet-4-5", 10, 5)

        from kiro.account_manager import account_label

        body = client.get("/api/dashboard/accounts/usage").json()
        assert body["usage"][account_label(path)]["email"] == "someone@example.com"

    def test_unattributed_bucket_is_reported_verbatim(self, client, dashboard):
        self._login(client)
        _record("key_a", None, "claude-sonnet-4-5", 10, 5)

        body = client.get("/api/dashboard/accounts/usage").json()
        assert body["usage"][UNKNOWN_ACCOUNT_ID]["totalTokens"] == 15
