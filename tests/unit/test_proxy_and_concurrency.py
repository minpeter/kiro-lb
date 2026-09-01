# -*- coding: utf-8 -*-
"""Proxy chain parsing and failover, plus the concurrency gate."""

from __future__ import annotations

import asyncio

import pytest

from kiro import concurrency, proxy_chain, store
from kiro.gateway_tunables import MAX_ACCOUNT_CONCURRENCY, MAX_CONCURRENCY, QUEUE_TIMEOUT_SECONDS


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    store.initialize()
    proxy_chain.reset_cache()
    concurrency.reset()
    for tunable in (MAX_CONCURRENCY, MAX_ACCOUNT_CONCURRENCY, QUEUE_TIMEOUT_SECONDS):
        tunable.reset_cache()
    yield
    proxy_chain.reset_cache()
    concurrency.reset()
    for tunable in (MAX_CONCURRENCY, MAX_ACCOUNT_CONCURRENCY, QUEUE_TIMEOUT_SECONDS):
        tunable.reset_cache()


class TestProxyParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("http://host:8080", "http://host:8080"),
            ("host:8080", "http://host:8080"),
            ("socks5://host:1080", "socks5://host:1080"),
            ("socks5h://host:1080", "socks5h://host:1080"),
            ("socks5|user:pass@host:1080", "socks5://user:pass@host:1080"),
        ],
    )
    def test_accepted_forms(self, raw, expected):
        assert proxy_chain.normalize(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "ftp://host:21", "gopher|host:70", 42, None])
    def test_rejected_forms(self, raw):
        with pytest.raises(proxy_chain.InvalidProxy):
            proxy_chain.normalize(raw)

    def test_password_is_masked(self):
        entry = proxy_chain.ProxyEntry("socks5://user:hunter2@host:1080")
        assert "hunter2" not in entry.masked
        assert "user:***@" in entry.masked

    def test_duplicates_collapse_keeping_order(self):
        urls = proxy_chain.validate(["a:1", "b:2", "a:1"])
        assert urls == ["http://a:1", "http://b:2"]

    def test_one_bad_entry_rejects_the_list(self):
        """Partial acceptance would silently route through fewer proxies."""
        with pytest.raises(proxy_chain.InvalidProxy):
            proxy_chain.validate(["http://good:1", "ftp://bad:2"])


class TestProxyChainState:
    def test_set_and_reload(self):
        proxy_chain.set_chain(["socks5://a:1080", "http://b:8080"])
        proxy_chain.reset_cache()
        entries = proxy_chain.load_from_store()
        assert [entry.url for entry in entries] == ["socks5://a:1080", "http://b:8080"]

    def test_empty_chain_means_direct(self):
        proxy_chain.set_chain([])
        assert proxy_chain.attempt_order() == []

    def test_failure_moves_an_entry_to_the_back(self):
        proxy_chain.set_chain(["http://a:1", "http://b:2"])
        proxy_chain.record_failure("http://a:1")
        assert [entry.url for entry in proxy_chain.attempt_order()] == ["http://b:2", "http://a:1"]

    def test_a_cooling_entry_is_never_dropped(self):
        """A cooldown must not shrink the chain to nothing."""
        proxy_chain.set_chain(["http://only:1"])
        proxy_chain.record_failure("http://only:1")
        assert len(proxy_chain.attempt_order()) == 1

    def test_success_clears_the_cooldown(self):
        proxy_chain.set_chain(["http://a:1"])
        proxy_chain.record_failure("http://a:1")
        proxy_chain.record_success("http://a:1")
        assert not proxy_chain.is_cooling("http://a:1")

    def test_a_corrupt_row_falls_back_to_direct(self):
        store.save_setting(proxy_chain.SETTING_KEY, ["ftp://nope:21"])
        proxy_chain.reset_cache()
        assert proxy_chain.load_from_store() == []


@pytest.mark.asyncio
class TestConcurrencyGate:
    async def test_disabled_by_default(self):
        assert MAX_CONCURRENCY.value() == 0
        async with concurrency.slot("acct"):
            pass

    async def test_global_limit_serialises(self):
        MAX_CONCURRENCY.set(1)
        QUEUE_TIMEOUT_SECONDS.set(5)
        concurrency.reset()

        order: list[str] = []

        async def worker(name: str):
            async with concurrency.slot():
                order.append(f"{name}-in")
                # Yield the loop so the other worker gets its chance to enter
                # while this slot is held; the gate is what must prevent it.
                await asyncio.sleep(0)
                order.append(f"{name}-out")

        await asyncio.gather(worker("a"), worker("b"))
        # With a limit of one, no worker may enter before the other has left.
        assert order in (
            ["a-in", "a-out", "b-in", "b-out"],
            ["b-in", "b-out", "a-in", "a-out"],
        )

    async def test_timeout_raises_instead_of_hanging(self):
        MAX_CONCURRENCY.set(1)
        QUEUE_TIMEOUT_SECONDS.set(1)
        concurrency.reset()

        held = asyncio.Event()
        release = asyncio.Event()

        async def hold():
            async with concurrency.slot():
                held.set()
                await release.wait()

        holder = asyncio.create_task(hold())
        await held.wait()
        with pytest.raises(concurrency.QueueTimeout):
            async with concurrency.slot():
                pass
        release.set()
        await holder

    async def test_status_reports_opaque_labels_not_raw_account_ids(self):
        """The status payload reaches API clients, and an account ID is a
        credential file path or a profile ARN, so the raw key must not appear."""
        from kiro.account_manager import account_label

        MAX_CONCURRENCY.set(0)
        MAX_ACCOUNT_CONCURRENCY.set(1)
        concurrency.reset()

        account_id = "arn:aws:codewhisperer:us-east-1:123456789012:profile/SECRET"
        held = asyncio.Event()
        release = asyncio.Event()

        async def hold():
            async with concurrency.slot(account_id):
                held.set()
                await release.wait()

        holder = asyncio.create_task(hold())
        await held.wait()
        keys = list(concurrency.status()["accounts"].keys())
        release.set()
        await holder

        assert keys == [account_label(account_id)]
        assert account_id not in keys
        assert not any("SECRET" in key for key in keys)

    async def test_the_route_reports_one_layer_of_masking(self):
        """Asserted through the route, not through concurrency.status(): the
        digest was applied in both layers at once, and a test that called the
        function directly could not see the route digesting a digest."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from kiro import dashboard
        from kiro.account_manager import account_label

        MAX_CONCURRENCY.set(0)
        MAX_ACCOUNT_CONCURRENCY.set(1)
        concurrency.reset()

        account_id = "arn:aws:codewhisperer:us-east-1:123456789012:profile/SECRET"
        expected = account_label(account_id)

        app = FastAPI()
        app.include_router(dashboard.router)
        original = dashboard._authenticated
        dashboard._authenticated = lambda _request: True
        try:
            held = asyncio.Event()
            release = asyncio.Event()

            async def hold():
                async with concurrency.slot(account_id):
                    held.set()
                    await release.wait()

            holder = asyncio.create_task(hold())
            await held.wait()
            with TestClient(app) as client:
                body = client.get("/api/dashboard/concurrency").json()
            release.set()
            await holder
        finally:
            dashboard._authenticated = original

        keys = list(body["accounts"].keys())
        assert keys == [expected], "the route must not mask an already masked key"
        assert account_id not in keys
        assert not any("SECRET" in key for key in keys)
        assert account_label(expected) not in keys

    async def test_per_account_limit_is_independent(self):
        MAX_CONCURRENCY.set(0)
        MAX_ACCOUNT_CONCURRENCY.set(1)
        QUEUE_TIMEOUT_SECONDS.set(5)
        concurrency.reset()

        running = {"a": 0, "b": 0}
        peak = {"a": 0, "b": 0}

        async def worker(account: str):
            async with concurrency.slot(account):
                running[account] += 1
                peak[account] = max(peak[account], running[account])
                await asyncio.sleep(0)
                running[account] -= 1

        await asyncio.gather(worker("a"), worker("a"), worker("b"), worker("b"))
        assert peak == {"a": 1, "b": 1}

    async def test_slot_is_released_when_the_body_raises(self):
        MAX_CONCURRENCY.set(1)
        QUEUE_TIMEOUT_SECONDS.set(2)
        concurrency.reset()

        with pytest.raises(RuntimeError):
            async with concurrency.slot():
                raise RuntimeError("boom")

        # A leaked slot would make this time out.
        async with concurrency.slot():
            pass
