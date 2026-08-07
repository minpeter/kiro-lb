# -*- coding: utf-8 -*-

"""
Integration tests for Account System failover flow.

Tests cover:
- Full failover between multiple accounts
- Sticky behavior (global index)
- Circuit Breaker with exponential backoff
- Half-Open recovery
- State persistence across restarts
- TTL refresh on usage
"""

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from kiro.account_errors import ErrorType
from kiro.account_manager import Account, AccountManager, account_label
from kiro.config import ACCOUNT_RECOVERY_TIMEOUT

# =============================================================================
# Integration Tests: Full Failover Flow
# =============================================================================


class TestAccountSystemFullFlow:
    """
    Integration tests for complete Account System flow.

    What it does: Tests end-to-end failover scenarios with multiple accounts
    Purpose: Verify Account System works correctly in realistic scenarios
    """

    @pytest.mark.asyncio
    async def test_full_failover_flow_two_accounts(
        self, tmp_path, temp_account_credentials_files, mock_list_models_response
    ):
        """
        Test 137: Полный failover между двумя аккаунтами

        What it does: Simulates complete failover from broken account to working one
        Purpose: Verify failover loop works end-to-end
        """
        print("\n=== Test 137: Full failover flow between two accounts ===")

        # Arrange: Create credentials.json with two accounts
        creds_file = tmp_path / "credentials.json"
        state_file = tmp_path / "state.json"

        account1_path = temp_account_credentials_files["account1"]
        account2_path = temp_account_credentials_files["account2"]

        credentials = [
            {"type": "json", "path": account1_path, "enabled": True},
            {"type": "json", "path": account2_path, "enabled": True},
        ]
        creds_file.write_text(json.dumps(credentials))
        from tests.conftest import seed_account_sources
        seed_account_sources(credentials)

        # Create AccountManager
        manager = AccountManager()
        await manager.load_credentials()
        await manager.load_state()

        print(f"Loaded {len(manager._accounts)} accounts")

        # Mock initialization for both accounts
        with patch.object(manager, "_initialize_account") as mock_init:

            async def mock_initialize(account_id):
                # Create mock components
                from kiro.auth import KiroAuthManager
                from kiro.cache import ModelInfoCache
                from kiro.model_resolver import ModelResolver

                account = manager._accounts[account_id]

                # Mock auth_manager
                auth_manager = MagicMock(spec=KiroAuthManager)
                auth_manager._access_token = f"token_{account_id}"
                auth_manager.q_host = "https://api.example.com"
                auth_manager.api_host = "https://api.example.com"

                # Mock model_cache with models
                model_cache = ModelInfoCache()
                await model_cache.update(mock_list_models_response["models"])

                # Mock model_resolver
                model_resolver = ModelResolver(cache=model_cache, hidden_models={}, aliases={}, hidden_from_list=set())

                account.auth_manager = auth_manager
                account.model_cache = model_cache
                account.model_resolver = model_resolver
                account.models_cached_at = time.time()

                # Update model_to_accounts
                for model in model_resolver.get_available_models():
                    if model not in manager._model_to_accounts:
                        from kiro.account_manager import ModelAccountList

                        manager._model_to_accounts[model] = ModelAccountList()
                    if account_id not in manager._model_to_accounts[model].accounts:
                        manager._model_to_accounts[model].accounts.append(account_id)

                return True

            mock_init.side_effect = mock_initialize

            # Initialize both accounts
            for account_id in list(manager._accounts.keys()):
                await manager._initialize_account(account_id)

        print(f"Initialized accounts: {list(manager._accounts.keys())}")

        # Act: Simulate failover scenario
        # 1. First account fails with RECOVERABLE error
        account1_id = list(manager._accounts.keys())[0]
        await manager.report_failure(account1_id, "claude-opus-4.5", ErrorType.RECOVERABLE, 429, None)
        print(f"Account 1 failed: failures={manager._accounts[account1_id].failures}")

        # 2. Get next account (should return account2)
        # Mock random.random() to disable probabilistic retry (make test deterministic)
        with patch("random.random", return_value=0.5):  # > 0.1 = no probabilistic retry
            next_account = await manager.get_next_account("claude-opus-4.5")
        account2_id = list(manager._accounts.keys())[1]

        print(f"Next account: {next_account.id if next_account else None}")
        assert next_account is not None
        assert next_account.id == account2_id

        # 3. Second account succeeds
        await manager.report_success(account2_id, "claude-opus-4.5")
        print(f"Account 2 succeeded: failures={manager._accounts[account2_id].failures}")

        # 4. While account1 is still cooling down it stays out of the rotation,
        # so account2 serves every request.
        with patch("random.random", return_value=0.5):
            for _ in range(10):
                candidate = await manager.get_next_account("claude-opus-4.5")
                assert candidate is not None
                assert candidate.id == account2_id

        # 5. Once the cooldown expires, account1 becomes reachable again.
        # Success no longer pins selection to account2: under quota-weighted
        # routing both accounts are selectable, and asserting a single winner
        # here would only re-encode the starvation this policy removed.
        manager._accounts[account1_id].failures = 0
        manager._accounts[account1_id].last_failure_time = 0.0

        seen = set()
        with patch("random.random", return_value=0.5):
            for _ in range(40):
                candidate = await manager.get_next_account("claude-opus-4.5")
                assert candidate is not None
                seen.add(candidate.id)

        print(f"Accounts reachable after recovery: {sorted(seen)}")
        assert seen == {account1_id, account2_id}

        print("✓ Full failover flow completed successfully")

    @pytest.mark.asyncio
    async def test_sticky_behavior_success_updates_index(
        self, tmp_path, temp_account_credentials_files, mock_list_models_response
    ):
        """
        Test 138: Sticky behavior обновляет global index

        What it does: Verifies global current_account_index is updated on success
        Purpose: Ensure sticky behavior works across all models
        """
        print("\n=== Test 138: Sticky behavior updates global index ===")

        # Arrange
        creds_file = tmp_path / "credentials.json"
        state_file = tmp_path / "state.json"

        account1_path = temp_account_credentials_files["account1"]
        account2_path = temp_account_credentials_files["account2"]

        credentials = [
            {"type": "json", "path": account1_path, "enabled": True},
            {"type": "json", "path": account2_path, "enabled": True},
        ]
        creds_file.write_text(json.dumps(credentials))
        from tests.conftest import seed_account_sources
        seed_account_sources(credentials)

        manager = AccountManager()
        await manager.load_credentials()
        await manager.load_state()

        # Initialize accounts (simplified)
        for account_id in list(manager._accounts.keys()):
            account = manager._accounts[account_id]
            account.auth_manager = MagicMock()
            account.model_cache = MagicMock()
            account.model_resolver = MagicMock()
            account.model_resolver.get_available_models.return_value = ["claude-opus-4.5"]
            account.models_cached_at = time.time()

            from kiro.account_manager import ModelAccountList

            if "claude-opus-4.5" not in manager._model_to_accounts:
                manager._model_to_accounts["claude-opus-4.5"] = ModelAccountList()
            manager._model_to_accounts["claude-opus-4.5"].accounts.append(account_id)

        print(f"Initial global index: {manager._current_account_index}")
        assert manager._current_account_index == 0

        # Act: Report success on second account
        account2_id = list(manager._accounts.keys())[1]
        await manager.report_success(account2_id, "claude-opus-4.5")

        # Assert: the last-success marker still tracks the account. It is no
        # longer the selection cursor under quota-weighted routing, but it is
        # the rotation start for the legacy sticky policy and part of the
        # persisted state document.
        print(f"Updated global index: {manager._current_account_index}")
        assert manager._current_account_index == 1
        print("✓ Last-success index was updated on success")

    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks_broken_account(self, tmp_path, temp_account_credentials_files, mock_time):
        """
        Test 139: Circuit Breaker блокирует сломанный аккаунт

        What it does: Verifies broken account is skipped during cooldown
        Purpose: Ensure Circuit Breaker prevents using broken accounts
        """
        print("\n=== Test 139: Circuit Breaker blocks broken account ===")

        # Arrange
        creds_file = tmp_path / "credentials.json"
        state_file = tmp_path / "state.json"

        account1_path = temp_account_credentials_files["account1"]
        account2_path = temp_account_credentials_files["account2"]

        credentials = [
            {"type": "json", "path": account1_path, "enabled": True},
            {"type": "json", "path": account2_path, "enabled": True},
        ]
        creds_file.write_text(json.dumps(credentials))
        from tests.conftest import seed_account_sources
        seed_account_sources(credentials)

        manager = AccountManager()
        await manager.load_credentials()
        await manager.load_state()

        # Initialize accounts
        for account_id in list(manager._accounts.keys()):
            account = manager._accounts[account_id]
            account.auth_manager = MagicMock()
            account.model_cache = MagicMock()
            account.model_resolver = MagicMock()
            account.model_resolver.get_available_models.return_value = ["claude-opus-4.5"]
            account.models_cached_at = time.time()

            from kiro.account_manager import ModelAccountList

            if "claude-opus-4.5" not in manager._model_to_accounts:
                manager._model_to_accounts["claude-opus-4.5"] = ModelAccountList()
            manager._model_to_accounts["claude-opus-4.5"].accounts.append(account_id)

        # Act: Break first account (5 failures)
        account1_id = list(manager._accounts.keys())[0]
        for i in range(5):
            await manager.report_failure(account1_id, "claude-opus-4.5", ErrorType.RECOVERABLE, 429, None)

        print(f"Account 1 failures: {manager._accounts[account1_id].failures}")
        print(f"Last failure time: {manager._accounts[account1_id].last_failure_time}")

        # Get next account - should skip account1 (in cooldown)
        with patch("random.random", return_value=0.5):  # Disable probabilistic retry
            next_account = await manager.get_next_account("claude-opus-4.5")

        account2_id = list(manager._accounts.keys())[1]
        print(f"Next account: {next_account.id if next_account else None}")
        assert next_account.id == account2_id
        print("✓ Broken account was skipped (Circuit Breaker)")

    @pytest.mark.asyncio
    async def test_half_open_recovery_after_timeout(self, tmp_path, temp_account_credentials_files):
        """
        Test 140: Half-Open восстанавливает аккаунт после timeout

        What it does: Verifies broken account is retried after recovery timeout
        Purpose: Ensure accounts can recover from broken state
        """
        print("\n=== Test 140: Half-Open recovery after timeout ===")

        # Arrange
        creds_file = tmp_path / "credentials.json"
        state_file = tmp_path / "state.json"

        account1_path = temp_account_credentials_files["account1"]

        credentials = [{"type": "json", "path": account1_path, "enabled": True}]
        creds_file.write_text(json.dumps(credentials))
        from tests.conftest import seed_account_sources
        seed_account_sources(credentials)

        manager = AccountManager()
        await manager.load_credentials()
        await manager.load_state()

        # Initialize account
        account_id = list(manager._accounts.keys())[0]
        account = manager._accounts[account_id]
        account.auth_manager = MagicMock()
        account.model_cache = MagicMock()
        account.model_resolver = MagicMock()
        account.model_resolver.get_available_models.return_value = ["claude-opus-4.5"]
        account.models_cached_at = time.time()

        from kiro.account_manager import ModelAccountList

        manager._model_to_accounts["claude-opus-4.5"] = ModelAccountList()
        manager._model_to_accounts["claude-opus-4.5"].accounts.append(account_id)

        # Act: Break account
        for i in range(3):
            await manager.report_failure(account_id, "claude-opus-4.5", ErrorType.RECOVERABLE, 429, None)

        print(f"Account failures: {account.failures}")
        print(f"Last failure time: {account.last_failure_time}")

        # Simulate time passing (recovery timeout)
        from kiro.config import ACCOUNT_MAX_BACKOFF_MULTIPLIER

        backoff_multiplier = min(2 ** (account.failures - 1), ACCOUNT_MAX_BACKOFF_MULTIPLIER)
        effective_timeout = ACCOUNT_RECOVERY_TIMEOUT * backoff_multiplier

        account.last_failure_time = time.time() - effective_timeout - 1
        print(f"Simulated time passing: {effective_timeout + 1}s")

        # Get next account - should return account (Half-Open)
        next_account = await manager.get_next_account("claude-opus-4.5")

        print(f"Next account (Half-Open): {next_account.id if next_account else None}")
        assert next_account is not None
        assert next_account.id == account_id
        print("✓ Account recovered via Half-Open state")

    @pytest.mark.asyncio
    async def test_state_persistence_across_restarts(self, tmp_path, temp_account_credentials_files):
        """
        Test 141: state.json сохраняется и восстанавливается

        What it does: Verifies state persists across manager restarts
        Purpose: Ensure runtime state survives restarts
        """
        print("\n=== Test 141: State persistence across restarts ===")

        # Arrange
        creds_file = tmp_path / "credentials.json"
        state_file = tmp_path / "state.json"

        account1_path = temp_account_credentials_files["account1"]

        credentials = [{"type": "json", "path": account1_path, "enabled": True}]
        creds_file.write_text(json.dumps(credentials))
        from tests.conftest import seed_account_sources
        seed_account_sources(credentials)

        # First manager instance
        manager1 = AccountManager()
        await manager1.load_credentials()
        await manager1.load_state()

        # Initialize account
        account_id = list(manager1._accounts.keys())[0]
        account = manager1._accounts[account_id]
        account.auth_manager = MagicMock()
        account.model_cache = MagicMock()
        account.model_resolver = MagicMock()
        account.model_resolver.get_available_models.return_value = ["claude-opus-4.5"]
        account.models_cached_at = 1704110400.0

        from kiro.account_manager import ModelAccountList

        manager1._model_to_accounts["claude-opus-4.5"] = ModelAccountList()
        manager1._model_to_accounts["claude-opus-4.5"].accounts.append(account_id)

        # Modify state
        account.failures = 3
        account.last_failure_time = 1704114000.0
        account.stats.total_requests = 100
        account.stats.successful_requests = 97
        account.stats.failed_requests = 3
        manager1._current_account_index = 0

        # Save state
        await manager1._save_state()
        print(f"Saved state: failures={account.failures}, stats={account.stats.total_requests}")

        # Second manager instance (restart simulation)
        manager2 = AccountManager()
        await manager2.load_credentials()
        await manager2.load_state()

        # Assert: State was restored
        account2 = manager2._accounts[account_id]
        print(f"Restored state: failures={account2.failures}, stats={account2.stats.total_requests}")

        assert account2.failures == 3
        assert account2.last_failure_time == 1704114000.0
        assert account2.models_cached_at == 1704110400.0
        assert account2.stats.total_requests == 100
        assert account2.stats.successful_requests == 97
        assert account2.stats.failed_requests == 3
        assert manager2._current_account_index == 0

        print("✓ State was persisted and restored correctly")

    @pytest.mark.asyncio
    async def test_ttl_refresh_on_usage(self, tmp_path, temp_account_credentials_files, mock_list_models_response):
        """
        Test 142: TTL обновляется только при использовании аккаунта

        What it does: Verifies model cache is refreshed when TTL expires during usage
        Purpose: Ensure cache stays fresh without background tasks
        """
        print("\n=== Test 142: TTL refresh on usage ===")

        # Arrange
        creds_file = tmp_path / "credentials.json"
        state_file = tmp_path / "state.json"

        account1_path = temp_account_credentials_files["account1"]

        credentials = [{"type": "json", "path": account1_path, "enabled": True}]
        creds_file.write_text(json.dumps(credentials))
        from tests.conftest import seed_account_sources
        seed_account_sources(credentials)

        manager = AccountManager()
        await manager.load_credentials()
        await manager.load_state()

        # Initialize account
        account_id = list(manager._accounts.keys())[0]
        account = manager._accounts[account_id]
        account.auth_manager = MagicMock()
        account.model_cache = MagicMock()
        account.model_resolver = MagicMock()
        account.model_resolver.get_available_models.return_value = ["claude-opus-4.5"]

        # Set old cache timestamp (expired TTL)
        from kiro.config import ACCOUNT_CACHE_TTL

        account.models_cached_at = time.time() - ACCOUNT_CACHE_TTL - 1
        print(f"Cache age: {time.time() - account.models_cached_at}s (TTL: {ACCOUNT_CACHE_TTL}s)")

        from kiro.account_manager import ModelAccountList

        manager._model_to_accounts["claude-opus-4.5"] = ModelAccountList()
        manager._model_to_accounts["claude-opus-4.5"].accounts.append(account_id)

        # Mock refresh method
        refresh_called = False
        original_cached_at = account.models_cached_at

        async def mock_refresh(acc_id):
            nonlocal refresh_called
            refresh_called = True
            manager._accounts[acc_id].models_cached_at = time.time()

        with patch.object(manager, "_refresh_account_models", side_effect=mock_refresh):
            # Act: Get account (should trigger TTL refresh)
            await manager.get_next_account("claude-opus-4.5")

        # Assert: Refresh was called and timestamp updated
        print(f"Refresh called: {refresh_called}")
        print(f"Old timestamp: {original_cached_at}")
        print(f"New timestamp: {account.models_cached_at}")

        assert refresh_called is True
        assert account.models_cached_at > original_cached_at
        print("✓ Cache was refreshed on usage when TTL expired")


# =============================================================================
# Integration Tests: 503 Diagnostics When The Pool Is Exhausted
# =============================================================================


class TestAccountSystemExhaustedPoolDiagnostics:
    """
    Verifies the client-visible 503 explains why no account was usable.

    A bare "No available accounts for this model." cannot be acted on: it does
    not say whether the pool is rate-limited, cooling down, or unauthenticated.
    These tests drive the real route handlers so a regression in the message
    fails here rather than in production logs.
    """

    def _exhausted_manager(self, tmp_path, account_count: int = 3) -> AccountManager:
        """Build a manager whose every account is inside its cooldown window."""
        manager = AccountManager()
        now = time.time()
        for index in range(account_count):
            account_id = f"/creds/account{index}.json"
            account = Account(id=account_id)
            account.auth_manager = MagicMock()
            account.failures = 2
            account.last_failure_time = now
            manager._accounts[account_id] = account
        return manager

    def _request(self, manager: AccountManager) -> MagicMock:
        request = MagicMock()
        request.app.state.account_manager = manager
        request.app.state.http_client = MagicMock()
        return request

    def _no_probabilistic_retry(self):
        """Pin the Circuit Breaker's 10% probabilistic retry to "skip".

        get_next_account() otherwise lets a cooling account through at random,
        which would make these assertions pass or fail by chance.
        """
        return patch("kiro.account_manager.random.random", return_value=1.0)

    @pytest.mark.asyncio
    async def test_openai_503_names_every_cooling_account(self, tmp_path):
        """
        What it does: Calls /v1/chat/completions logic with a fully cooling pool
        Purpose: The 503 detail must name each account and its cooldown reason
        """
        print("\n=== Test: OpenAI 503 reports pool state ===")

        from kiro.models_openai import ChatCompletionRequest
        from kiro.routes_openai import chat_completions

        manager = self._exhausted_manager(tmp_path)
        request_data = ChatCompletionRequest(
            model="claude-sonnet-4-5", messages=[{"role": "user", "content": "hi"}], stream=False
        )

        with self._no_probabilistic_retry():
            with pytest.raises(HTTPException) as exc_info:
                await chat_completions(self._request(manager), request_data)

        detail = exc_info.value.detail
        print(f"Status: {exc_info.value.status_code}")
        print(f"Detail: {detail}")

        assert exc_info.value.status_code == 503
        assert "No available accounts for this model." in detail
        for account_id in manager._accounts:
            assert f"{account_label(account_id)}: cooling down for" in detail
        # Credential paths must not reach the client
        assert "/creds/" not in detail

    @pytest.mark.asyncio
    async def test_anthropic_503_names_every_cooling_account(self, tmp_path):
        """
        What it does: Calls /v1/messages logic with a fully cooling pool
        Purpose: Anthropic must carry the same diagnostics as OpenAI
        """
        print("\n=== Test: Anthropic 503 reports pool state ===")

        from kiro.models_anthropic import AnthropicMessagesRequest
        from kiro.routes_anthropic import messages

        manager = self._exhausted_manager(tmp_path)
        request_data = AnthropicMessagesRequest(
            model="claude-sonnet-4-5", max_tokens=64, messages=[{"role": "user", "content": "hi"}], stream=False
        )

        with self._no_probabilistic_retry():
            response = await messages(self._request(manager), request_data)

        body = json.loads(bytes(response.body))
        message = body["error"]["message"]
        print(f"Status: {response.status_code}")
        print(f"Message: {message}")

        assert response.status_code == 503
        assert "No available accounts for this model." in message
        for account_id in manager._accounts:
            assert f"{account_label(account_id)}: cooling down for" in message
        assert "/creds/" not in message

    @pytest.mark.asyncio
    async def test_503_distinguishes_uninitialized_from_cooling(self, tmp_path):
        """
        What it does: Mixes a cooling account with an account that fails to init
        Purpose: The operator must be able to tell auth failures from rate limits
        """
        print("\n=== Test: 503 distinguishes uninitialized accounts ===")

        from kiro.models_openai import ChatCompletionRequest
        from kiro.routes_openai import chat_completions

        manager = self._exhausted_manager(tmp_path, account_count=1)
        broken_id = "/creds/broken.json"
        manager._accounts[broken_id] = Account(id=broken_id)

        cooling_id = "/creds/account0.json"

        # Initialization failure keeps auth_manager None, so the account is
        # reported as not initialized rather than rate-limited.
        request_data = ChatCompletionRequest(
            model="claude-sonnet-4-5", messages=[{"role": "user", "content": "hi"}], stream=False
        )
        with (
            self._no_probabilistic_retry(),
            patch.object(manager, "_initialize_account", AsyncMock(return_value=False)),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await chat_completions(self._request(manager), request_data)

        detail = exc_info.value.detail
        print(f"Detail: {detail}")

        assert exc_info.value.status_code == 503
        assert f"{account_label(cooling_id)}: cooling down for" in detail
        assert f"{account_label(broken_id)}: not initialized" in detail


class TestFailoverTokenAttribution:
    """Tokens belong to the account that answered, not the one first tried.

    This is the fact the old counters could not express. `AccountStats` increments
    once per attempt, so a request that failed over across four accounts looked
    like four requests with no way to tell they were one; and the token table had
    no account axis at all. Attribution is now carried in a ContextVar written per
    attempt, so the last write - the account that actually produced the response -
    is the one the tokens land on.
    """

    def _record_for(self, account_id, model="claude-sonnet-4.5", prompt=10, completion=5):
        from kiro.usage_tracking import current_account_id, current_api_key_id, record_token_usage

        current_api_key_id.set("key_a")
        current_account_id.set(account_id)
        record_token_usage(model, prompt, completion)

    def _record_against_current(self, model="claude-sonnet-4.5", prompt=10, completion=5):
        """Record without re-setting the account, reading whatever the context holds.

        The concurrency test needs this: re-setting the account immediately before
        recording would defeat the interleaving it is trying to expose, and the
        test would pass even against a shared global.
        """
        from kiro.usage_tracking import current_api_key_id, record_token_usage

        current_api_key_id.set("key_a")
        record_token_usage(model, prompt, completion)

    def test_tokens_land_on_the_account_that_answered(self):
        from kiro.usage_tracking import current_account_id, drain_pending_usage

        drain_pending_usage()
        try:
            # Two attempts fail before a third serves. Only the third produced
            # tokens, so only it may be charged for them.
            current_account_id.set("/creds/failed-one.json")
            current_account_id.set("/creds/failed-two.json")
            self._record_for("/creds/served.json")

            drained = drain_pending_usage()
            assert [(row[1], row[3], row[4]) for row in drained] == [("/creds/served.json", 10, 5)]
        finally:
            drain_pending_usage()
            current_account_id.set(None)

    def test_two_requests_served_by_different_accounts_split(self):
        from kiro.usage_tracking import current_account_id, drain_pending_usage

        drain_pending_usage()
        try:
            self._record_for("/creds/one.json", completion=5)
            self._record_for("/creds/two.json", completion=7)

            drained = {row[1]: row[4] for row in drain_pending_usage()}
            assert drained == {"/creds/one.json": 5, "/creds/two.json": 7}
        finally:
            drain_pending_usage()
            current_account_id.set(None)

    @pytest.mark.asyncio
    async def test_attribution_survives_concurrent_requests(self):
        """Two in-flight requests on different accounts must not cross-attribute.

        A ContextVar is the reason this holds: a module-level variable would be
        shared, and whichever request finished last would silently claim the
        other's tokens.
        """
        import asyncio

        from kiro.usage_tracking import current_account_id, drain_pending_usage

        drain_pending_usage()

        async def serve(account_id, completion):
            current_account_id.set(account_id)
            # Yield control so the other coroutine sets its own account in
            # between, then record against whatever this task's context still
            # holds. Re-setting it here would hide a shared-state bug.
            await asyncio.sleep(0)
            self._record_against_current(completion=completion)

        try:
            await asyncio.gather(
                serve("/creds/one.json", 11),
                serve("/creds/two.json", 22),
            )

            drained = {row[1]: row[4] for row in drain_pending_usage()}
            assert drained == {"/creds/one.json": 11, "/creds/two.json": 22}
        finally:
            drain_pending_usage()
            current_account_id.set(None)


class TestFailoverAttributionThroughTheRoute:
    """The route's per-attempt attribution, exercised through the failover loop.

    The tests above pin the accounting primitive; this one pins the wiring. A
    change that moved `current_account_id.set` out of the loop - or dropped it -
    would leave those passing while every token landed on the wrong account, so
    the assertion here is specifically that the *second* account owns the tokens
    after the first one fails.
    """

    @pytest.mark.asyncio
    async def test_the_account_that_answered_owns_the_tokens(self, monkeypatch, tmp_path):
        import json as json_module

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from kiro import routes_openai
        from kiro.account_manager import AccountManager
        from kiro.usage_tracking import (
            current_account_id,
            current_api_key_id,
            drain_pending_usage,
            record_token_usage,
        )

        # Two accounts, both initialized, in a manager the route will select from.
        entries = [
            {"type": "json", "path": str(tmp_path / f"{name}.json"), "enabled": True} for name in ("first", "second")
        ]
        for entry in entries:
            with open(entry["path"], "w") as handle:
                json_module.dump({"accessToken": "t", "refreshToken": "r"}, handle)
        credentials = tmp_path / "credentials.json"
        with open(credentials, "w") as handle:
            json_module.dump(entries, handle)
        from tests.conftest import seed_account_sources
        seed_account_sources(entries)

        manager = AccountManager()
        await manager.load_credentials()
        for account_id in list(manager._accounts.keys()):
            account = manager._accounts[account_id]
            account.auth_manager = MagicMock()
            account.auth_manager.profile_arn = None
            account.auth_manager.auth_type = None
            account.auth_manager.get_access_token = AsyncMock(return_value="token")
            account.model_cache = MagicMock()
            account.initialized = True
            account.is_initializing = False
        first_id, second_id = list(manager._accounts.keys())

        # The first attempt gets a 429, which classify_error treats as RECOVERABLE
        # so the loop moves to the next account; the second succeeds. A 500 would
        # be FATAL and returned to the client without any failover, which is what
        # the first version of this test hit.
        attempts: list[str] = []

        class FakeResponse:
            def __init__(self, status_code):
                self.status_code = status_code
                self.headers = {}

            async def aread(self):
                return b'{"message": "upstream exploded"}'

        async def fake_request_with_retry(self, *args, **kwargs):
            # Whichever account the loop set is the one this attempt belongs to.
            attempts.append(current_account_id.get())
            return FakeResponse(429 if len(attempts) == 1 else 200)

        async def fake_collect(*args, **kwargs):
            # Stands in for the serializer, which is where tokens are really
            # recorded. Deliberately does NOT set the account: it reads whatever
            # the route left in the ContextVar, which is the thing under test.
            record_token_usage("claude-sonnet-4.5", 100, 20)
            return {"id": "x", "choices": [], "usage": {}}

        monkeypatch.setattr(routes_openai.KiroHttpClient, "request_with_retry", fake_request_with_retry)
        monkeypatch.setattr(routes_openai.KiroHttpClient, "close", AsyncMock())
        monkeypatch.setattr(routes_openai.KiroHttpClient, "client", MagicMock(), raising=False)
        monkeypatch.setattr(routes_openai, "collect_stream_response", fake_collect)
        monkeypatch.setattr(routes_openai, "build_kiro_payload", lambda *a, **k: {"payload": True})
        app = FastAPI()
        app.include_router(routes_openai.router)
        app.state.account_manager = manager
        app.state.http_client = MagicMock()
        # The route reads the multi-account failover switch off app.state, not
        # config, and the loop under test only runs when it is on.
        # Auth is a separate concern with its own tests; overridden so this one
        # fails only on attribution.
        app.dependency_overrides[routes_openai.verify_api_key] = lambda: True

        drain_pending_usage()
        # Recording needs a key as well as an account; authentication normally
        # sets it and is stubbed out here.
        current_api_key_id.set("key_a")
        try:
            with patch("random.random", return_value=0.5):
                with TestClient(app) as client:
                    response = client.post(
                        "/v1/chat/completions",
                        headers={"Authorization": "Bearer test-key"},
                        json={
                            "model": "claude-sonnet-4.5",
                            "messages": [{"role": "user", "content": "hi"}],
                            "stream": False,
                        },
                    )

            assert response.status_code == 200
            # The loop really did fail over: two attempts, two different accounts.
            assert len(attempts) == 2
            assert attempts[0] != attempts[1]

            drained = drain_pending_usage()
            attributed = {row[1] for row in drained}
            # Exactly one account is charged, and it is the one that answered -
            # not the first one tried.
            assert attributed == {attempts[1]}
            assert first_id not in attributed or second_id not in attributed
        finally:
            drain_pending_usage()
            current_account_id.set(None)
