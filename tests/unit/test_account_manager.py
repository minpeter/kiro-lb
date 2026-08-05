# -*- coding: utf-8 -*-

"""
Tests for kiro/account_manager.py - Unified Account System.

Tests the AccountManager class that manages multiple Kiro accounts with:
- Lazy initialization
- Sticky behavior (prefer successful account)
- Circuit breaker with exponential backoff
- TTL-based model cache refresh
- State persistence
"""

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from kiro.account_errors import ErrorType
from kiro.account_manager import Account, AccountManager, AccountStats, _format_duration, account_label
from kiro.config import (
    ACCOUNT_QUOTA_QUARANTINE,
    ACCOUNT_RATE_LIMIT_COOLDOWN,
    ACCOUNT_RECOVERY_TIMEOUT,
)


class TestAccountDataclass:
    """
    Tests for Account and AccountStats dataclasses.
    """

    def test_account_creation_with_defaults(self):
        """
        Test Account creation with default values.

        What it does: Verifies Account dataclass initialization
        Purpose: Ensure default values are set correctly
        """
        print("\n=== Test: Account creation with defaults ===")

        # Act
        account = Account(id="/test/path.json")

        # Assert
        print(f"Account ID: {account.id}")
        print(f"Auth manager: {account.auth_manager}")
        print(f"Failures: {account.failures}")
        print(f"Last failure time: {account.last_failure_time}")

        assert account.id == "/test/path.json"
        assert account.auth_manager is None
        assert account.model_cache is None
        assert account.model_resolver is None
        assert account.failures == 0
        assert account.last_failure_time == 0.0
        assert account.models_cached_at == 0.0
        assert isinstance(account.stats, AccountStats)

    def test_account_stats_initialization(self):
        """
        Test AccountStats initialization with zeros.

        What it does: Verifies AccountStats default values
        Purpose: Ensure statistics start at zero
        """
        print("\n=== Test: AccountStats initialization ===")

        # Act
        stats = AccountStats()

        # Assert
        print(f"Total requests: {stats.total_requests}")
        print(f"Successful requests: {stats.successful_requests}")
        print(f"Failed requests: {stats.failed_requests}")

        assert stats.total_requests == 0
        assert stats.successful_requests == 0
        assert stats.failed_requests == 0


class TestAccountManagerLoadCredentials:
    """
    Tests for AccountManager.load_credentials() method.
    """

    @pytest.mark.asyncio
    async def test_load_credentials_json_type(self, tmp_path):
        """
        Test loading credentials with type=json.

        What it does: Loads single JSON credential file
        Purpose: Verify JSON type credential loading
        """
        print("\n=== Test: load_credentials with type=json ===")

        # Arrange
        creds_file = tmp_path / "credentials.json"
        test_json = tmp_path / "test.json"
        test_json.write_text(
            json.dumps(
                {"refreshToken": "test_token", "accessToken": "test_access", "expiresAt": "2099-01-01T00:00:00.000Z"}
            )
        )

        credentials = [{"type": "json", "path": str(test_json), "enabled": True}]
        creds_file.write_text(json.dumps(credentials))

        manager = AccountManager(credentials_file=str(creds_file), state_file=str(tmp_path / "state.json"))

        # Act
        await manager.load_credentials()

        # Assert
        print(f"Loaded accounts: {len(manager._accounts)}")
        print(f"Account IDs: {list(manager._accounts.keys())}")

        assert len(manager._accounts) == 1
        assert str(test_json.resolve()) in manager._accounts

    @pytest.mark.asyncio
    async def test_load_credentials_sqlite_type(self, tmp_path, temp_sqlite_db):
        """
        Test loading credentials with type=sqlite.

        What it does: Loads SQLite database credential
        Purpose: Verify SQLite type credential loading
        """
        print("\n=== Test: load_credentials with type=sqlite ===")

        # Arrange
        creds_file = tmp_path / "credentials.json"
        credentials = [{"type": "sqlite", "path": temp_sqlite_db, "enabled": True}]
        creds_file.write_text(json.dumps(credentials))

        manager = AccountManager(credentials_file=str(creds_file), state_file=str(tmp_path / "state.json"))

        # Act
        await manager.load_credentials()

        # Assert
        print(f"Loaded accounts: {len(manager._accounts)}")

        assert len(manager._accounts) == 1
        assert str(Path(temp_sqlite_db).resolve()) in manager._accounts

    @pytest.mark.asyncio
    async def test_load_credentials_refresh_token_type(self, tmp_path):
        """
        Test loading credentials with type=refresh_token.

        What it does: Loads refresh token credential
        Purpose: Verify refresh_token type credential loading
        """
        print("\n=== Test: load_credentials with type=refresh_token ===")

        # Arrange
        creds_file = tmp_path / "credentials.json"
        credentials = [
            {
                "type": "refresh_token",
                "refresh_token": "test_refresh_token_abc123",
                "profile_arn": "arn:aws:codewhisperer:us-east-1:123456789:profile/test",
                "region": "us-east-1",
                "enabled": True,
            }
        ]
        creds_file.write_text(json.dumps(credentials))

        # Create state file to avoid errors
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"current_account_index": 0, "model_to_accounts": {}, "accounts": {}}))

        manager = AccountManager(credentials_file=str(creds_file), state_file=str(state_file))

        # Act
        await manager.load_credentials()

        # Assert
        print(f"Loaded accounts: {len(manager._accounts)}")
        print(f"Account IDs: {list(manager._accounts.keys())}")

        assert len(manager._accounts) == 1
        # refresh_token type uses deterministic hash as ID
        account_id = list(manager._accounts.keys())[0]
        assert account_id.startswith("refresh_token_")

    @pytest.mark.asyncio
    async def test_load_credentials_folder_scanning(self, tmp_path):
        """
        Test folder scanning for credential files.

        What it does: Scans folder and loads all valid credential files
        Purpose: Verify folder scanning functionality
        """
        print("\n=== Test: load_credentials with folder scanning ===")

        # Arrange
        folder = tmp_path / "accounts"
        folder.mkdir()

        # Create valid files
        file1 = folder / "account1.json"
        file1.write_text(
            json.dumps({"refreshToken": "token1", "accessToken": "access1", "expiresAt": "2099-01-01T00:00:00.000Z"})
        )

        file2 = folder / "account2.json"
        file2.write_text(
            json.dumps({"refreshToken": "token2", "accessToken": "access2", "expiresAt": "2099-01-01T00:00:00.000Z"})
        )

        creds_file = tmp_path / "credentials.json"
        credentials = [{"type": "json", "path": str(folder), "enabled": True}]
        creds_file.write_text(json.dumps(credentials))

        manager = AccountManager(credentials_file=str(creds_file), state_file=str(tmp_path / "state.json"))

        # Act
        await manager.load_credentials()

        # Assert
        print(f"Loaded accounts: {len(manager._accounts)}")

        assert len(manager._accounts) == 2

    @pytest.mark.asyncio
    async def test_load_credentials_skip_invalid_files(self, tmp_path):
        """
        Test that invalid files are skipped with WARNING.

        What it does: Loads folder with invalid files
        Purpose: Verify invalid files are skipped gracefully
        """
        print("\n=== Test: load_credentials skips invalid files ===")

        # Arrange
        folder = tmp_path / "accounts"
        folder.mkdir()

        # Valid file
        valid_file = folder / "valid.json"
        valid_file.write_text(
            json.dumps({"refreshToken": "token", "accessToken": "access", "expiresAt": "2099-01-01T00:00:00.000Z"})
        )

        # Invalid JSON
        invalid_file = folder / "invalid.json"
        invalid_file.write_text("not a valid json {{{")

        # Non-JSON file
        text_file = folder / "readme.txt"
        text_file.write_text("This is not a credential file")

        creds_file = tmp_path / "credentials.json"
        credentials = [{"type": "json", "path": str(folder), "enabled": True}]
        creds_file.write_text(json.dumps(credentials))

        manager = AccountManager(credentials_file=str(creds_file), state_file=str(tmp_path / "state.json"))

        # Act
        await manager.load_credentials()

        # Assert
        print(f"Loaded accounts: {len(manager._accounts)}")

        assert len(manager._accounts) == 1  # Only valid file loaded

    @pytest.mark.asyncio
    async def test_load_credentials_skip_disabled(self, tmp_path):
        """
        Test that entries with enabled=false are skipped.

        What it does: Loads credentials with disabled entry
        Purpose: Verify enabled flag is respected
        """
        print("\n=== Test: load_credentials skips disabled entries ===")

        # Arrange
        test_json = tmp_path / "test.json"
        test_json.write_text(
            json.dumps({"refreshToken": "token", "accessToken": "access", "expiresAt": "2099-01-01T00:00:00.000Z"})
        )

        creds_file = tmp_path / "credentials.json"
        credentials = [
            {
                "type": "json",
                "path": str(test_json),
                "enabled": False,  # Disabled
            }
        ]
        creds_file.write_text(json.dumps(credentials))

        manager = AccountManager(credentials_file=str(creds_file), state_file=str(tmp_path / "state.json"))

        # Act
        await manager.load_credentials()

        # Assert
        print(f"Loaded accounts: {len(manager._accounts)}")

        assert len(manager._accounts) == 0

    @pytest.mark.asyncio
    async def test_load_credentials_missing_type(self, tmp_path):
        """
        Test that entries without type are skipped.

        What it does: Loads credentials with missing type field
        Purpose: Verify type validation
        """
        print("\n=== Test: load_credentials skips entries without type ===")

        # Arrange
        creds_file = tmp_path / "credentials.json"
        credentials = [
            {
                "path": "/some/path.json",
                "enabled": True,
                # Missing "type" field
            }
        ]
        creds_file.write_text(json.dumps(credentials))

        manager = AccountManager(credentials_file=str(creds_file), state_file=str(tmp_path / "state.json"))

        # Act
        await manager.load_credentials()

        # Assert
        print(f"Loaded accounts: {len(manager._accounts)}")

        assert len(manager._accounts) == 0

    @pytest.mark.asyncio
    async def test_load_credentials_missing_path(self, tmp_path):
        """
        Test that json/sqlite entries without path are skipped.

        What it does: Loads credentials with missing path field
        Purpose: Verify path validation for json/sqlite types
        """
        print("\n=== Test: load_credentials skips json/sqlite without path ===")

        # Arrange
        creds_file = tmp_path / "credentials.json"
        credentials = [
            {
                "type": "json",
                "enabled": True,
                # Missing "path" field
            }
        ]
        creds_file.write_text(json.dumps(credentials))

        manager = AccountManager(credentials_file=str(creds_file), state_file=str(tmp_path / "state.json"))

        # Act
        await manager.load_credentials()

        # Assert
        print(f"Loaded accounts: {len(manager._accounts)}")

        assert len(manager._accounts) == 0

    @pytest.mark.asyncio
    async def test_load_credentials_missing_refresh_token(self, tmp_path):
        """
        Test that refresh_token entries without refresh_token field are skipped.

        What it does: Loads credentials with missing refresh_token field
        Purpose: Verify refresh_token validation
        """
        print("\n=== Test: load_credentials skips refresh_token without token ===")

        # Arrange
        creds_file = tmp_path / "credentials.json"
        credentials = [
            {
                "type": "refresh_token",
                "profile_arn": "arn:aws:codewhisperer:us-east-1:123456789:profile/test",
                "enabled": True,
                # Missing "refresh_token" field
            }
        ]
        creds_file.write_text(json.dumps(credentials))

        manager = AccountManager(credentials_file=str(creds_file), state_file=str(tmp_path / "state.json"))

        # Act
        await manager.load_credentials()

        # Assert
        print(f"Loaded accounts: {len(manager._accounts)}")

        assert len(manager._accounts) == 0

    @pytest.mark.asyncio
    async def test_load_credentials_file_not_found(self, tmp_path):
        """
        Test handling of non-existent credentials.json.

        What it does: Attempts to load non-existent file
        Purpose: Verify graceful handling of missing file
        """
        print("\n=== Test: load_credentials with missing file ===")

        # Arrange
        manager = AccountManager(
            credentials_file=str(tmp_path / "nonexistent.json"), state_file=str(tmp_path / "state.json")
        )

        # Act
        await manager.load_credentials()

        # Assert
        print(f"Loaded accounts: {len(manager._accounts)}")

        assert len(manager._accounts) == 0


class TestAccountManagerLoadState:
    """
    Tests for AccountManager.load_state() method.
    """

    @pytest.mark.asyncio
    async def test_load_state_success(self, tmp_path, sample_state_with_data):
        """
        Test loading existing state.json.

        What it does: Loads state from file
        Purpose: Verify state restoration
        """
        print("\n=== Test: load_state success ===")

        # Arrange
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps(sample_state_with_data))

        # Create accounts first
        test_json = tmp_path / "test.json"
        test_json.write_text(json.dumps({"refreshToken": "token"}))

        creds_file = tmp_path / "credentials.json"
        creds_file.write_text(json.dumps([{"type": "json", "path": str(test_json), "enabled": True}]))

        manager = AccountManager(credentials_file=str(creds_file), state_file=str(state_file))

        await manager.load_credentials()

        # Act
        await manager.load_state()

        # Assert
        print(f"Model mappings: {len(manager._model_to_accounts)}")
        print(f"Current account index: {manager._current_account_index}")

        assert len(manager._model_to_accounts) > 0

    @pytest.mark.asyncio
    async def test_load_state_restore_current_account_index(self, tmp_path):
        """
        Test restoration of global current_account_index.

        What it does: Restores sticky index from state
        Purpose: Verify global sticky behavior persistence
        """
        print("\n=== Test: load_state restores current_account_index ===")

        # Arrange
        state_data = {"current_account_index": 2, "model_to_accounts": {}, "accounts": {}}

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps(state_data))

        manager = AccountManager(credentials_file=str(tmp_path / "creds.json"), state_file=str(state_file))

        # Act
        await manager.load_state()

        # Assert
        print(f"Current account index: {manager._current_account_index}")

        assert manager._current_account_index == 2

    @pytest.mark.asyncio
    async def test_load_state_restore_model_to_accounts(self, tmp_path):
        """
        Test restoration of model_to_accounts mapping.

        What it does: Restores model mappings from state
        Purpose: Verify model-to-account mapping persistence
        """
        print("\n=== Test: load_state restores model_to_accounts ===")

        # Arrange
        state_data = {
            "current_account_index": 0,
            "model_to_accounts": {"claude-opus-4.5": {"accounts": ["/test/account1.json", "/test/account2.json"]}},
            "accounts": {},
        }

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps(state_data))

        manager = AccountManager(credentials_file=str(tmp_path / "creds.json"), state_file=str(state_file))

        # Act
        await manager.load_state()

        # Assert
        print(f"Model mappings: {manager._model_to_accounts}")

        assert "claude-opus-4.5" in manager._model_to_accounts
        assert len(manager._model_to_accounts["claude-opus-4.5"].accounts) == 2

    @pytest.mark.asyncio
    async def test_load_state_restore_account_runtime_state(self, tmp_path):
        """
        Test restoration of account runtime state (failures, stats, etc).

        What it does: Restores account state from file
        Purpose: Verify runtime state persistence
        """
        print("\n=== Test: load_state restores account runtime state ===")

        # Arrange
        # Create account first to get correct resolved path
        test_json = tmp_path / "account.json"
        test_json.write_text(json.dumps({"refreshToken": "token"}))
        account_id = str(test_json.resolve())

        state_data = {
            "current_account_index": 0,
            "model_to_accounts": {},
            "accounts": {
                account_id: {
                    "failures": 3,
                    "last_failure_time": 1704110400.0,
                    "models_cached_at": 1704106800.0,
                    "stats": {"total_requests": 100, "successful_requests": 97, "failed_requests": 3},
                }
            },
        }

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps(state_data))

        creds_file = tmp_path / "credentials.json"
        creds_file.write_text(json.dumps([{"type": "json", "path": str(test_json), "enabled": True}]))

        manager = AccountManager(credentials_file=str(creds_file), state_file=str(state_file))

        await manager.load_credentials()

        # Act
        await manager.load_state()

        # Assert
        account = manager._accounts[account_id]
        print(f"Account failures: {account.failures}")
        print(f"Account stats: {account.stats}")

        assert account.failures == 3
        assert account.last_failure_time == 1704110400.0
        assert account.models_cached_at == 1704106800.0
        assert account.stats.total_requests == 100

    @pytest.mark.asyncio
    async def test_load_state_file_not_found(self, tmp_path):
        """
        Test handling of non-existent state.json (empty state).

        What it does: Attempts to load non-existent state file
        Purpose: Verify graceful handling with empty state
        """
        print("\n=== Test: load_state with missing file ===")

        # Arrange
        manager = AccountManager(
            credentials_file=str(tmp_path / "creds.json"), state_file=str(tmp_path / "nonexistent.json")
        )

        # Act
        await manager.load_state()

        # Assert
        print(f"Model mappings: {len(manager._model_to_accounts)}")
        print(f"Current account index: {manager._current_account_index}")

        assert len(manager._model_to_accounts) == 0
        assert manager._current_account_index == 0

    @pytest.mark.asyncio
    async def test_load_state_corrupted_json(self, tmp_path):
        """
        Test handling of corrupted state.json.

        What it does: Attempts to load invalid JSON
        Purpose: Verify error handling for corrupted state
        """
        print("\n=== Test: load_state with corrupted JSON ===")

        # Arrange
        state_file = tmp_path / "state.json"
        state_file.write_text("not a valid json {{{")

        manager = AccountManager(credentials_file=str(tmp_path / "creds.json"), state_file=str(state_file))

        # Act
        await manager.load_state()

        # Assert - should handle gracefully
        print(f"Model mappings: {len(manager._model_to_accounts)}")

        assert len(manager._model_to_accounts) == 0


class TestAccountManagerInitializeAccount:
    """
    Tests for AccountManager._initialize_account() method.
    """

    def test_internal_source_matches_only_its_explicit_id(self, tmp_path):
        manager = AccountManager(str(tmp_path / "credentials.json"), str(tmp_path / "state.json"))
        internal = {"type": "internal", "id": "internal-1"}
        file_source = {"type": "json", "path": str(tmp_path / "account.json")}
        manager._credentials_config = [internal, file_source]

        assert manager._credentials_for_account("internal-1") is internal
        assert manager._credentials_for_account(str((tmp_path / "account.json").resolve())) is file_source
        assert manager._credentials_for_account(str(tmp_path.resolve())) is None

    @pytest.mark.asyncio
    async def test_deleted_account_is_not_committed_after_initialization(self, tmp_path):
        manager = AccountManager(str(tmp_path / "credentials.json"), str(tmp_path / "state.json"))
        account_id = "internal-1"
        source = {"type": "internal", "id": account_id}
        manager._credentials_config = [source]
        manager._accounts[account_id] = Account(id=account_id)
        token_started = asyncio.Event()
        allow_token = asyncio.Event()

        async def delayed_token(_auth):
            token_started.set()
            await allow_token.wait()
            return "token"

        with patch("kiro.account_manager.KiroAuthManager.get_access_token", delayed_token):
            task = asyncio.create_task(manager.initialize_account(account_id))
            await token_started.wait()
            async with manager._lock:
                manager._remove_account_state(account_id)
                manager._credentials_config.clear()
            allow_token.set()
            assert await task is False

        assert account_id not in manager._accounts
        assert all(account_id not in mapping.accounts for mapping in manager._model_to_accounts.values())

    @pytest.mark.asyncio
    async def test_initialize_account_json_success(self, tmp_path, mock_list_models_response):
        """
        Test successful account initialization with type=json.

        What it does: Initializes account with JSON credentials
        Purpose: Verify complete initialization flow
        """
        print("\n=== Test: initialize_account with JSON ===")

        # Arrange
        test_json = tmp_path / "test.json"
        test_json.write_text(
            json.dumps(
                {
                    "refreshToken": "test_token",
                    "accessToken": "test_access",
                    "expiresAt": "2099-01-01T00:00:00.000Z",
                    "profileArn": "arn:aws:codewhisperer:us-east-1:123456789:profile/test",
                    "region": "us-east-1",
                }
            )
        )

        creds_file = tmp_path / "credentials.json"
        creds_file.write_text(json.dumps([{"type": "json", "path": str(test_json), "enabled": True}]))

        manager = AccountManager(credentials_file=str(creds_file), state_file=str(tmp_path / "state.json"))

        await manager.load_credentials()
        account_id = str(test_json.resolve())

        # Mock HTTP client for ListAvailableModels
        with patch("kiro.account_manager.KiroHttpClient") as mock_http_class:
            mock_client = AsyncMock()
            mock_response = Mock()  # Response is not async
            mock_response.status_code = 200
            mock_response.json.return_value = mock_list_models_response
            mock_client.request_with_retry = AsyncMock(return_value=mock_response)
            mock_client.close = AsyncMock()
            mock_http_class.return_value = mock_client

            # Act
            success = await manager._initialize_account(account_id)

        # Assert
        print(f"Initialization success: {success}")
        assert success is True
        assert manager._accounts[account_id].auth_manager is not None
        assert manager._accounts[account_id].model_cache is not None
        assert manager._accounts[account_id].model_resolver is not None

    @pytest.mark.asyncio
    async def test_initialize_account_fetch_models_fallback(self, tmp_path):
        """
        Test fallback to FALLBACK_MODELS when API fails.

        What it does: Initializes account when ListAvailableModels fails
        Purpose: Verify fallback mechanism
        """
        print("\n=== Test: initialize_account with fallback models ===")

        # Arrange
        test_json = tmp_path / "test.json"
        test_json.write_text(
            json.dumps(
                {"refreshToken": "test_token", "accessToken": "test_access", "expiresAt": "2099-01-01T00:00:00.000Z"}
            )
        )

        creds_file = tmp_path / "credentials.json"
        creds_file.write_text(json.dumps([{"type": "json", "path": str(test_json), "enabled": True}]))

        manager = AccountManager(credentials_file=str(creds_file), state_file=str(tmp_path / "state.json"))

        await manager.load_credentials()
        account_id = str(test_json.resolve())

        # Mock HTTP client to fail
        with patch("kiro.account_manager.KiroHttpClient") as mock_http_class:
            mock_client = AsyncMock()
            mock_client.request_with_retry = AsyncMock(side_effect=Exception("Network error"))
            mock_client.close = AsyncMock()
            mock_http_class.return_value = mock_client

            # Act
            success = await manager._initialize_account(account_id)

        # Assert
        print(f"Initialization success: {success}")
        assert success is True  # Should succeed with fallback
        assert manager._accounts[account_id].model_cache is not None


class TestAccountManagerGetNextAccount:
    """
    Tests for AccountManager.get_next_account() method.
    """

    @pytest.mark.asyncio
    async def test_get_next_account_single_bypass_circuit_breaker(self, tmp_path, mock_list_models_response):
        """
        Test that single account bypasses Circuit Breaker.

        What it does: Gets account when only one exists
        Purpose: Verify single account always returns (no cooldown)
        """
        print("\n=== Test: get_next_account single account bypasses Circuit Breaker ===")

        # Arrange
        test_json = tmp_path / "test.json"
        test_json.write_text(
            json.dumps(
                {"refreshToken": "test_token", "accessToken": "test_access", "expiresAt": "2099-01-01T00:00:00.000Z"}
            )
        )

        creds_file = tmp_path / "credentials.json"
        creds_file.write_text(json.dumps([{"type": "json", "path": str(test_json), "enabled": True}]))

        manager = AccountManager(credentials_file=str(creds_file), state_file=str(tmp_path / "state.json"))

        await manager.load_credentials()
        account_id = str(test_json.resolve())

        # Initialize account
        with patch("kiro.account_manager.KiroHttpClient") as mock_http_class:
            mock_client = AsyncMock()
            mock_response = Mock()  # Response is not async
            mock_response.status_code = 200
            mock_response.json.return_value = mock_list_models_response
            mock_client.request_with_retry = AsyncMock(return_value=mock_response)
            mock_client.close = AsyncMock()
            mock_http_class.return_value = mock_client

            await manager._initialize_account(account_id)

        # Set failures (should be ignored for single account)
        manager._accounts[account_id].failures = 10
        manager._accounts[account_id].last_failure_time = time.time()

        # Act
        account = await manager.get_next_account("claude-opus-4.5")

        # Assert
        print(f"Got account: {account is not None}")
        assert account is not None  # Single account always returns


class TestAccountManagerRateLimitCooldown:
    """
    Tests for the 429 USER_REQUEST_RATE_EXCEEDED path.

    A rate rejection must rotate the account out briefly without feeding the
    Circuit Breaker: escalating a momentary burst into exponential backoff
    shrinks the usable pool (observed live: 1m -> 2m -> ... -> 1h in 4 minutes).
    """

    def _pool(self, tmp_path, account_count: int = 2) -> AccountManager:
        manager = AccountManager(
            credentials_file=str(tmp_path / "credentials.json"), state_file=str(tmp_path / "state.json")
        )
        for index in range(account_count):
            account_id = f"/creds/account{index}.json"
            account = Account(id=account_id)
            account.auth_manager = MagicMock()
            account.models_cached_at = time.time()
            manager._accounts[account_id] = account
        return manager

    @pytest.mark.asyncio
    async def test_rate_limit_does_not_increment_failures(self, tmp_path):
        """
        What it does: Reports repeated 429 USER_REQUEST_RATE_EXCEEDED
        Purpose: The Circuit Breaker must stay untouched no matter how many
                 rate rejections arrive, so backoff cannot escalate
        """
        print("\n=== Test: rate limit leaves failure counter alone ===")

        manager = self._pool(tmp_path, account_count=1)
        account_id = "/creds/account0.json"

        for _ in range(5):
            await manager.report_failure(
                account_id, "claude-sonnet-4-5", ErrorType.RECOVERABLE, 429, "USER_REQUEST_RATE_EXCEEDED"
            )

        account = manager._accounts[account_id]
        print(f"Failures: {account.failures}, failed_requests: {account.stats.failed_requests}")

        assert account.failures == 0
        assert account.last_failure_time == 0.0
        # The failure is still counted as a failed request for the operator
        assert account.stats.failed_requests == 5
        assert account.stats.total_requests == 5

    @pytest.mark.asyncio
    async def test_rate_limit_sets_short_fixed_window(self, tmp_path):
        """
        What it does: Reports one 429 and inspects the cooldown window
        Purpose: The window must be the fixed short cooldown, not a backoff step
        """
        print("\n=== Test: rate limit uses a short fixed window ===")

        manager = self._pool(tmp_path, account_count=1)
        account_id = "/creds/account0.json"

        before = time.time()
        await manager.report_failure(
            account_id, "claude-sonnet-4-5", ErrorType.RECOVERABLE, 429, "USER_REQUEST_RATE_EXCEEDED"
        )

        remaining = manager._accounts[account_id].rate_limited_until - before
        print(f"Cooldown window: {remaining:.1f}s (configured {ACCOUNT_RATE_LIMIT_COOLDOWN}s)")

        assert 0 < remaining <= ACCOUNT_RATE_LIMIT_COOLDOWN + 1
        assert remaining < ACCOUNT_RECOVERY_TIMEOUT

    @pytest.mark.asyncio
    async def test_rate_limited_account_is_skipped_then_returns(self, tmp_path):
        """
        What it does: Rate limits one account, then expires the window
        Purpose: Selection must exclude the account for the whole window and
                 make it reachable again afterwards at full health
        """
        print("\n=== Test: rate-limited account is skipped, then reused ===")

        manager = self._pool(tmp_path, account_count=2)
        first, second = "/creds/account0.json", "/creds/account1.json"

        await manager.report_failure(
            first, "claude-sonnet-4-5", ErrorType.RECOVERABLE, 429, "USER_REQUEST_RATE_EXCEEDED"
        )

        # Within the window: every attempt lands on the other account. Routing
        # order is randomized by quota weight, so the exclusion is asserted over
        # repeated draws rather than a single deterministic pick.
        for _ in range(20):
            selected = await manager.get_next_account("claude-sonnet-4-5")
            assert selected is not None
            assert selected.id == second
        print(f"During window selected: {second} on every attempt")

        # Expire the window without sleeping.
        manager._accounts[first].rate_limited_until = time.time() - 1

        seen = set()
        for _ in range(40):
            selected = await manager.get_next_account("claude-sonnet-4-5")
            assert selected is not None
            seen.add(selected.id)
        print(f"After window reachable: {sorted(seen)}")
        assert first in seen
        assert manager._accounts[first].failures == 0

    @pytest.mark.asyncio
    async def test_rate_limited_account_has_no_probabilistic_retry(self, tmp_path):
        """
        What it does: Rate limits the only usable account and forces the
                      Circuit Breaker's retry dice to "retry"
        Purpose: Retrying a rate-limited account only earns another 429, so the
                 probabilistic escape hatch must not apply to this state
        """
        print("\n=== Test: rate-limit window ignores probabilistic retry ===")

        manager = self._pool(tmp_path, account_count=2)
        first, second = "/creds/account0.json", "/creds/account1.json"

        for account_id in (first, second):
            await manager.report_failure(
                account_id, "claude-sonnet-4-5", ErrorType.RECOVERABLE, 429, "USER_REQUEST_RATE_EXCEEDED"
            )

        with patch("kiro.account_manager.random.random", return_value=0.0):
            selected = await manager.get_next_account("claude-sonnet-4-5")

        print(f"Selected: {selected}")
        assert selected is None

    @pytest.mark.asyncio
    async def test_success_clears_rate_limit_window(self, tmp_path):
        """
        What it does: Rate limits an account, then reports a success
        Purpose: A success proves the account accepts requests again, so the
                 leftover window must not keep it parked
        """
        print("\n=== Test: success clears the rate-limit window ===")

        manager = self._pool(tmp_path, account_count=2)
        account_id = "/creds/account0.json"

        await manager.report_failure(
            account_id, "claude-sonnet-4-5", ErrorType.RECOVERABLE, 429, "USER_REQUEST_RATE_EXCEEDED"
        )
        assert manager._accounts[account_id].rate_limited_until > time.time()

        await manager.report_success(account_id, "claude-sonnet-4-5")

        print(f"rate_limited_until: {manager._accounts[account_id].rate_limited_until}")
        assert manager._accounts[account_id].rate_limited_until == 0.0


class TestAccountManagerQuotaExclusion:
    """
    Tests for the 402 MONTHLY_REQUEST_COUNT path.

    A quota-exhausted account cannot serve anything until its quota resets, so
    it must leave the rotation entirely instead of absorbing probabilistic
    retries that can only ever return another 402.
    """

    def _pool(self, tmp_path, account_count: int = 2) -> AccountManager:
        manager = AccountManager(
            credentials_file=str(tmp_path / "credentials.json"), state_file=str(tmp_path / "state.json")
        )
        for index in range(account_count):
            account_id = f"/creds/account{index}.json"
            account = Account(id=account_id)
            account.auth_manager = MagicMock()
            account.models_cached_at = time.time()
            manager._accounts[account_id] = account
        return manager

    @pytest.mark.asyncio
    async def test_quota_exhaustion_quarantines_without_failure_count(self, tmp_path):
        """
        What it does: Reports 402 MONTHLY_REQUEST_COUNT
        Purpose: The account is excluded by quarantine, not by an ever-growing
                 Circuit Breaker counter
        """
        print("\n=== Test: quota exhaustion sets quarantine ===")

        manager = self._pool(tmp_path, account_count=1)
        account_id = "/creds/account0.json"

        before = time.time()
        await manager.report_failure(
            account_id, "claude-sonnet-4-5", ErrorType.RECOVERABLE, 402, "MONTHLY_REQUEST_COUNT"
        )

        account = manager._accounts[account_id]
        remaining = account.quota_exhausted_until - before
        print(f"Quarantine: {remaining:.0f}s, failures: {account.failures}")

        assert 0 < remaining <= ACCOUNT_QUOTA_QUARANTINE + 1
        assert account.failures == 0
        assert account.stats.failed_requests == 1

    @pytest.mark.asyncio
    async def test_quota_exhausted_account_is_never_selected(self, tmp_path):
        """
        What it does: Exhausts one account, forces the retry dice to "retry"
        Purpose: Unlike a Circuit Breaker cooldown, the quarantine has no
                 probabilistic escape hatch - a 402 account must not be picked
        """
        print("\n=== Test: quota-exhausted account is excluded from routing ===")

        manager = self._pool(tmp_path, account_count=2)
        dead, alive = "/creds/account0.json", "/creds/account1.json"

        await manager.report_failure(dead, "claude-sonnet-4-5", ErrorType.RECOVERABLE, 402, "MONTHLY_REQUEST_COUNT")

        with patch("kiro.account_manager.random.random", return_value=0.0):
            for _ in range(10):
                selected = await manager.get_next_account("claude-sonnet-4-5")
                assert selected is not None
                assert selected.id == alive

        print(f"All 10 selections routed to {alive}")

    @pytest.mark.asyncio
    async def test_quota_exhausted_pool_returns_none(self, tmp_path):
        """
        What it does: Exhausts every account in the pool
        Purpose: Selection must report "nothing usable" rather than hand back a
                 402 account and burn a live request
        """
        print("\n=== Test: fully exhausted pool yields no account ===")

        manager = self._pool(tmp_path, account_count=2)
        for account_id in list(manager._accounts):
            await manager.report_failure(
                account_id, "claude-sonnet-4-5", ErrorType.RECOVERABLE, 402, "MONTHLY_REQUEST_COUNT"
            )

        with patch("kiro.account_manager.random.random", return_value=0.0):
            selected = await manager.get_next_account("claude-sonnet-4-5")

        print(f"Selected: {selected}")
        assert selected is None

    @pytest.mark.asyncio
    async def test_quota_quarantine_expires_and_account_returns(self, tmp_path):
        """
        What it does: Expires the quarantine window
        Purpose: A quota reset must bring the account back without operator action
        """
        print("\n=== Test: quarantine expiry restores the account ===")

        manager = self._pool(tmp_path, account_count=2)
        recovered = "/creds/account0.json"

        await manager.report_failure(
            recovered, "claude-sonnet-4-5", ErrorType.RECOVERABLE, 402, "MONTHLY_REQUEST_COUNT"
        )

        # Still quarantined: the account must not be selected at all.
        for _ in range(20):
            selected = await manager.get_next_account("claude-sonnet-4-5")
            assert selected is not None
            assert selected.id != recovered

        manager._accounts[recovered].quota_exhausted_until = time.time() - 1

        # Reachable again. Selection order is quota-weighted and randomized, so
        # reachability is asserted over repeated draws rather than one pick.
        seen = set()
        for _ in range(40):
            selected = await manager.get_next_account("claude-sonnet-4-5")
            assert selected is not None
            seen.add(selected.id)
        print(f"Reachable after expiry: {sorted(seen)}")

        assert recovered in seen

    @pytest.mark.asyncio
    async def test_success_clears_quota_quarantine(self, tmp_path):
        """
        What it does: Quarantines an account, then reports a success
        Purpose: A served request proves the quota is back, so the remaining
                 quarantine must not keep the account parked
        """
        print("\n=== Test: success clears the quota quarantine ===")

        manager = self._pool(tmp_path, account_count=2)
        account_id = "/creds/account0.json"

        await manager.report_failure(
            account_id, "claude-sonnet-4-5", ErrorType.RECOVERABLE, 402, "MONTHLY_REQUEST_COUNT"
        )
        assert manager._accounts[account_id].quota_exhausted_until > time.time()

        await manager.report_success(account_id, "claude-sonnet-4-5")

        print(f"quota_exhausted_until: {manager._accounts[account_id].quota_exhausted_until}")
        assert manager._accounts[account_id].quota_exhausted_until == 0.0

    @pytest.mark.asyncio
    async def test_quota_quarantine_survives_restart(self, tmp_path):
        """
        What it does: Saves state, then loads it into a fresh manager
        Purpose: Quota exhaustion outlives the process, so a restart must not
                 hand the dead account traffic again
        """
        print("\n=== Test: quarantine is persisted across restart ===")

        creds_file = tmp_path / "credentials.json"
        state_file = tmp_path / "state.json"
        account_id = "/creds/account0.json"

        manager = self._pool(tmp_path, account_count=2)
        manager._state_file = str(state_file)
        await manager.report_failure(
            account_id, "claude-sonnet-4-5", ErrorType.RECOVERABLE, 402, "MONTHLY_REQUEST_COUNT"
        )
        saved_until = manager._accounts[account_id].quota_exhausted_until
        await manager._save_state()

        # Fresh process: same credentials, state reloaded from disk.
        restarted = AccountManager(credentials_file=str(creds_file), state_file=str(state_file))
        restarted._accounts = {
            "/creds/account0.json": Account(id="/creds/account0.json"),
            "/creds/account1.json": Account(id="/creds/account1.json"),
        }
        await restarted.load_state()

        reloaded = restarted._accounts[account_id].quota_exhausted_until
        print(f"Saved: {saved_until}, reloaded: {reloaded}")

        assert reloaded == saved_until
        assert reloaded > time.time()


class TestAccountManagerDescribePoolState:
    """
    Tests for AccountManager.describe_pool_state() - 503 diagnostics.
    """

    def _manager(self, tmp_path) -> AccountManager:
        return AccountManager(
            credentials_file=str(tmp_path / "credentials.json"), state_file=str(tmp_path / "state.json")
        )

    def test_describe_pool_state_empty_pool(self, tmp_path):
        """
        Test description when no accounts are configured.

        What it does: Describes an empty pool
        Purpose: The 503 must say the pool is empty rather than stay silent
        """
        print("\n=== Test: describe_pool_state with empty pool ===")

        manager = self._manager(tmp_path)

        description = manager.describe_pool_state()
        print(f"Description: {description}")

        assert description == "no accounts configured"

    def test_describe_pool_state_reports_cooldown_and_exclusion(self, tmp_path):
        """
        Test that cooldown, already-tried, and uninitialized states are named.

        What it does: Builds a mixed pool and inspects the description
        Purpose: An operator must be able to tell a rate-limit burst apart
                 from cooldowns or auth failures from the 503 body alone
        """
        print("\n=== Test: describe_pool_state reports per-account reasons ===")

        manager = self._manager(tmp_path)
        manager._accounts = {
            "/creds/cooling.json": Account(id="/creds/cooling.json"),
            "/creds/tried.json": Account(id="/creds/tried.json"),
            "/creds/fresh.json": Account(id="/creds/fresh.json"),
            "/creds/ready.json": Account(id="/creds/ready.json"),
        }

        # Cooling down: 1 failure -> ACCOUNT_RECOVERY_TIMEOUT (60s default)
        manager._accounts["/creds/cooling.json"].failures = 1
        manager._accounts["/creds/cooling.json"].last_failure_time = time.time()

        # Already used up its attempt in this request
        manager._accounts["/creds/tried.json"].auth_manager = MagicMock()

        # Initialized and healthy
        manager._accounts["/creds/ready.json"].auth_manager = MagicMock()

        description = manager.describe_pool_state({"/creds/tried.json"})
        print(f"Description: {description}")

        cooling_label = account_label("/creds/cooling.json")
        tried_label = account_label("/creds/tried.json")
        fresh_label = account_label("/creds/fresh.json")
        ready_label = account_label("/creds/ready.json")

        assert f"{cooling_label}: cooling down for" in description
        assert f"{tried_label}: already tried in this request" in description
        assert f"{fresh_label}: not initialized" in description
        assert f"{ready_label}: available" in description

        # Credential paths must never leak to a client-visible error
        assert "/creds/" not in description

    def test_describe_pool_state_reports_rate_limit_separately(self, tmp_path):
        """
        Test that a rate-limited account is not reported as available.

        What it does: Parks an account in its rate-limit window
        Purpose: The 503 must name a rate-limit burst as such, since it clears
                 in seconds while a cooldown does not
        """
        print("\n=== Test: describe_pool_state reports rate limiting ===")

        manager = self._manager(tmp_path)
        manager._accounts = {"/creds/burst.json": Account(id="/creds/burst.json")}
        manager._accounts["/creds/burst.json"].auth_manager = MagicMock()
        manager._accounts["/creds/burst.json"].rate_limited_until = time.time() + 10

        description = manager.describe_pool_state()
        print(f"Description: {description}")

        assert f"{account_label('/creds/burst.json')}: rate limited for" in description
        assert "cooling down" not in description

    def test_describe_pool_state_reports_quota_exhaustion(self, tmp_path):
        """
        Test that a quota-exhausted account is named as such.

        What it does: Quarantines an account for quota exhaustion
        Purpose: A 402 exclusion lasts hours, so the 503 must not present it as
                 a transient rate limit or cooldown
        """
        print("\n=== Test: describe_pool_state reports quota exhaustion ===")

        manager = self._manager(tmp_path)
        manager._accounts = {"/creds/empty.json": Account(id="/creds/empty.json")}
        manager._accounts["/creds/empty.json"].auth_manager = MagicMock()
        manager._accounts["/creds/empty.json"].quota_exhausted_until = time.time() + 3600

        description = manager.describe_pool_state()
        print(f"Description: {description}")

        assert f"{account_label('/creds/empty.json')}: monthly quota exhausted, excluded for" in description
        assert "rate limited" not in description
        assert "cooling down" not in description

    def test_describe_pool_state_expired_cooldown_is_not_reported_as_cooling(self, tmp_path):
        """
        Test that an account past its backoff window is not shown as cooling down.

        What it does: Sets a stale failure timestamp
        Purpose: Half-Open accounts are selectable, so the message must not
                 claim they are still in cooldown
        """
        print("\n=== Test: describe_pool_state with expired cooldown ===")

        manager = self._manager(tmp_path)
        manager._accounts = {"/creds/recovered.json": Account(id="/creds/recovered.json")}
        manager._accounts["/creds/recovered.json"].failures = 1
        manager._accounts["/creds/recovered.json"].last_failure_time = time.time() - ACCOUNT_RECOVERY_TIMEOUT - 1
        manager._accounts["/creds/recovered.json"].auth_manager = MagicMock()

        description = manager.describe_pool_state()
        print(f"Description: {description}")

        assert description == f"{account_label('/creds/recovered.json')}: available"


class TestAccountManagerReportSuccess:
    """
    Tests for AccountManager.report_success() method.
    """

    @pytest.mark.asyncio
    async def test_report_success_reset_failures(self, tmp_path, mock_list_models_response):
        """
        Test that report_success resets failures to 0.

        What it does: Reports success after failures
        Purpose: Verify failure counter reset
        """
        print("\n=== Test: report_success resets failures ===")

        # Arrange
        test_json = tmp_path / "test.json"
        test_json.write_text(
            json.dumps(
                {"refreshToken": "test_token", "accessToken": "test_access", "expiresAt": "2099-01-01T00:00:00.000Z"}
            )
        )

        creds_file = tmp_path / "credentials.json"
        creds_file.write_text(json.dumps([{"type": "json", "path": str(test_json), "enabled": True}]))

        manager = AccountManager(credentials_file=str(creds_file), state_file=str(tmp_path / "state.json"))

        await manager.load_credentials()
        account_id = str(test_json.resolve())

        # Initialize account
        with patch("kiro.account_manager.KiroHttpClient") as mock_http_class:
            mock_client = AsyncMock()
            mock_response = Mock()  # Response is not async
            mock_response.status_code = 200
            mock_response.json.return_value = mock_list_models_response
            mock_client.request_with_retry = AsyncMock(return_value=mock_response)
            mock_client.close = AsyncMock()
            mock_http_class.return_value = mock_client

            await manager._initialize_account(account_id)

        # Set failures
        manager._accounts[account_id].failures = 5

        # Act
        await manager.report_success(account_id, "claude-opus-4.5")

        # Assert
        print(f"Failures after success: {manager._accounts[account_id].failures}")
        assert manager._accounts[account_id].failures == 0

    @pytest.mark.asyncio
    async def test_report_success_update_stats(self, tmp_path, mock_list_models_response):
        """
        Test that report_success updates statistics.

        What it does: Reports success and checks stats
        Purpose: Verify statistics tracking
        """
        print("\n=== Test: report_success updates stats ===")

        # Arrange
        test_json = tmp_path / "test.json"
        test_json.write_text(
            json.dumps(
                {"refreshToken": "test_token", "accessToken": "test_access", "expiresAt": "2099-01-01T00:00:00.000Z"}
            )
        )

        creds_file = tmp_path / "credentials.json"
        creds_file.write_text(json.dumps([{"type": "json", "path": str(test_json), "enabled": True}]))

        manager = AccountManager(credentials_file=str(creds_file), state_file=str(tmp_path / "state.json"))

        await manager.load_credentials()
        account_id = str(test_json.resolve())

        # Initialize account
        with patch("kiro.account_manager.KiroHttpClient") as mock_http_class:
            mock_client = AsyncMock()
            mock_response = Mock()  # Response is not async
            mock_response.status_code = 200
            mock_response.json.return_value = mock_list_models_response
            mock_client.request_with_retry = AsyncMock(return_value=mock_response)
            mock_client.close = AsyncMock()
            mock_http_class.return_value = mock_client

            await manager._initialize_account(account_id)

        # Act
        await manager.report_success(account_id, "claude-opus-4.5")

        # Assert
        stats = manager._accounts[account_id].stats
        print(f"Stats: total={stats.total_requests}, successful={stats.successful_requests}")
        assert stats.total_requests == 1
        assert stats.successful_requests == 1


class TestAccountManagerReportFailure:
    """
    Tests for AccountManager.report_failure() method.
    """

    @pytest.mark.asyncio
    async def test_report_failure_recoverable_increment_failures(self, tmp_path, mock_list_models_response):
        """
        Test that RECOVERABLE errors increment failures.

        What it does: Reports RECOVERABLE failure
        Purpose: Verify failure counter increment
        """
        print("\n=== Test: report_failure RECOVERABLE increments failures ===")

        # Arrange
        test_json = tmp_path / "test.json"
        test_json.write_text(
            json.dumps(
                {"refreshToken": "test_token", "accessToken": "test_access", "expiresAt": "2099-01-01T00:00:00.000Z"}
            )
        )

        creds_file = tmp_path / "credentials.json"
        creds_file.write_text(json.dumps([{"type": "json", "path": str(test_json), "enabled": True}]))

        manager = AccountManager(credentials_file=str(creds_file), state_file=str(tmp_path / "state.json"))

        await manager.load_credentials()
        account_id = str(test_json.resolve())

        # Initialize account
        with patch("kiro.account_manager.KiroHttpClient") as mock_http_class:
            mock_client = AsyncMock()
            mock_response = Mock()  # Response is not async
            mock_response.status_code = 200
            mock_response.json.return_value = mock_list_models_response
            mock_client.request_with_retry = AsyncMock(return_value=mock_response)
            mock_client.close = AsyncMock()
            mock_http_class.return_value = mock_client

            await manager._initialize_account(account_id)

        # Act
        await manager.report_failure(account_id, "claude-opus-4.5", ErrorType.RECOVERABLE, 429, None)

        # Assert
        print(f"Failures: {manager._accounts[account_id].failures}")
        assert manager._accounts[account_id].failures == 1

    @pytest.mark.asyncio
    async def test_report_failure_fatal_no_increment(self, tmp_path, mock_list_models_response):
        """
        Test that FATAL errors do NOT increment failures.

        What it does: Reports FATAL failure
        Purpose: Verify failures not incremented for request errors
        """
        print("\n=== Test: report_failure FATAL does not increment failures ===")

        # Arrange
        test_json = tmp_path / "test.json"
        test_json.write_text(
            json.dumps(
                {"refreshToken": "test_token", "accessToken": "test_access", "expiresAt": "2099-01-01T00:00:00.000Z"}
            )
        )

        creds_file = tmp_path / "credentials.json"
        creds_file.write_text(json.dumps([{"type": "json", "path": str(test_json), "enabled": True}]))

        manager = AccountManager(credentials_file=str(creds_file), state_file=str(tmp_path / "state.json"))

        await manager.load_credentials()
        account_id = str(test_json.resolve())

        # Initialize account
        with patch("kiro.account_manager.KiroHttpClient") as mock_http_class:
            mock_client = AsyncMock()
            mock_response = Mock()  # Response is not async
            mock_response.status_code = 200
            mock_response.json.return_value = mock_list_models_response
            mock_client.request_with_retry = AsyncMock(return_value=mock_response)
            mock_client.close = AsyncMock()
            mock_http_class.return_value = mock_client

            await manager._initialize_account(account_id)

        # Act
        await manager.report_failure(
            account_id, "claude-opus-4.5", ErrorType.FATAL, 400, "CONTENT_LENGTH_EXCEEDS_THRESHOLD"
        )

        # Assert
        print(f"Failures: {manager._accounts[account_id].failures}")
        assert manager._accounts[account_id].failures == 0  # Not incremented


class TestAccountManagerSaveState:
    """
    Tests for AccountManager._save_state() and save_state_periodically().
    """

    @pytest.mark.asyncio
    async def test_non_writer_skip_reports_false_and_preserves_dirty(self, tmp_path):
        manager = AccountManager(str(tmp_path / "credentials.json"), str(tmp_path / "state.json"))
        manager._dirty = True

        with patch("kiro.store.save_runtime_state", return_value=False):
            assert await manager._save_state() is False
            assert manager._dirty is True
            with pytest.raises(RuntimeError, match="not the active writer"):
                await manager._save_state(raise_errors=True)

    @pytest.mark.asyncio
    async def test_save_state_atomic_write(self, tmp_path):
        """
        Test atomic state saving via tmp file.

        What it does: Saves state and checks tmp file usage
        Purpose: Verify atomic write pattern
        """
        print("\n=== Test: save_state atomic write ===")

        # Arrange
        state_file = tmp_path / "state.json"
        manager = AccountManager(credentials_file=str(tmp_path / "creds.json"), state_file=str(state_file))

        # Act
        await manager._save_state()

        # Assert: the unified store transaction publishes the complete document.
        from kiro.store import load_runtime_state

        saved = load_runtime_state()
        assert saved is not None
        assert saved["current_account_index"] == 0
        assert saved["accounts"] == {}
        assert not state_file.exists()


class TestAccountManagerGetFirstAccount:
    """
    Tests for AccountManager.get_first_account() method.
    """

    @pytest.mark.asyncio
    async def test_get_first_account_success(self, tmp_path, mock_list_models_response):
        """
        Test getting first initialized account.

        What it does: Gets first account for legacy mode
        Purpose: Verify legacy mode support
        """
        print("\n=== Test: get_first_account success ===")

        # Arrange
        test_json = tmp_path / "test.json"
        test_json.write_text(
            json.dumps(
                {"refreshToken": "test_token", "accessToken": "test_access", "expiresAt": "2099-01-01T00:00:00.000Z"}
            )
        )

        creds_file = tmp_path / "credentials.json"
        creds_file.write_text(json.dumps([{"type": "json", "path": str(test_json), "enabled": True}]))

        manager = AccountManager(credentials_file=str(creds_file), state_file=str(tmp_path / "state.json"))

        await manager.load_credentials()
        account_id = str(test_json.resolve())

        # Initialize account
        with patch("kiro.account_manager.KiroHttpClient") as mock_http_class:
            mock_client = AsyncMock()
            mock_response = Mock()  # Response is not async
            mock_response.status_code = 200
            mock_response.json.return_value = mock_list_models_response
            mock_client.request_with_retry = AsyncMock(return_value=mock_response)
            mock_client.close = AsyncMock()
            mock_http_class.return_value = mock_client

            await manager._initialize_account(account_id)

        # Act
        account = manager.get_first_account()

        # Assert
        print(f"Got account: {account is not None}")
        assert account is not None
        assert account.auth_manager is not None

    def test_get_first_account_no_initialized(self, tmp_path):
        """
        Test RuntimeError when no initialized accounts.

        What it does: Attempts to get account when none initialized
        Purpose: Verify error handling
        """
        print("\n=== Test: get_first_account with no initialized accounts ===")

        # Arrange
        manager = AccountManager(credentials_file=str(tmp_path / "creds.json"), state_file=str(tmp_path / "state.json"))

        # Act & Assert
        with pytest.raises(RuntimeError, match="No initialized accounts available"):
            manager.get_first_account()


class TestAccountManagerGetAllAvailableModels:
    """
    Tests for AccountManager.get_all_available_models() method.
    """

    @pytest.mark.asyncio
    async def test_get_all_available_models_collect_from_all(self, tmp_path, mock_list_models_response):
        """
        Test collecting unique models from all accounts.

        What it does: Gets models from multiple accounts
        Purpose: Verify model aggregation for /v1/models endpoint
        """
        print("\n=== Test: get_all_available_models collects from all ===")

        # Arrange
        test_json = tmp_path / "test.json"
        test_json.write_text(
            json.dumps(
                {"refreshToken": "test_token", "accessToken": "test_access", "expiresAt": "2099-01-01T00:00:00.000Z"}
            )
        )

        creds_file = tmp_path / "credentials.json"
        creds_file.write_text(json.dumps([{"type": "json", "path": str(test_json), "enabled": True}]))

        manager = AccountManager(credentials_file=str(creds_file), state_file=str(tmp_path / "state.json"))

        await manager.load_credentials()
        account_id = str(test_json.resolve())

        # Initialize account
        with patch("kiro.account_manager.KiroHttpClient") as mock_http_class:
            mock_client = AsyncMock()
            mock_response = Mock()  # Response is not async
            mock_response.status_code = 200
            mock_response.json.return_value = mock_list_models_response
            mock_client.request_with_retry = AsyncMock(return_value=mock_response)
            mock_client.close = AsyncMock()
            mock_http_class.return_value = mock_client

            await manager._initialize_account(account_id)

        # Act
        models = manager.get_all_available_models()

        # Assert
        print(f"Available models: {len(models)}")
        assert len(models) > 0
        assert isinstance(models, list)
        assert all(isinstance(m, str) for m in models)


class TestFormatDuration:
    """
    Tests for _format_duration() helper function.
    """

    def test_format_duration_seconds(self):
        """Test formatting seconds."""
        assert _format_duration(30) == "30s"
        assert _format_duration(59) == "59s"

    def test_format_duration_minutes(self):
        """Test formatting minutes."""
        assert _format_duration(60) == "1m"
        assert _format_duration(300) == "5m"
        assert _format_duration(3599) == "59m"

    def test_format_duration_hours(self):
        """Test formatting hours."""
        assert _format_duration(3600) == "1h"
        assert _format_duration(7200) == "2h"
        assert _format_duration(86399) == "23h"

    def test_format_duration_days(self):
        """Test formatting days."""
        assert _format_duration(86400) == "1d"
        assert _format_duration(172800) == "2d"


class TestQuotaWeightedRouting:
    """
    Tests for quota-weighted account selection.

    Replaces the global sticky index, whose cursor only advanced on success: an
    account that was never selected could never succeed, so it was never
    selected. Live symptom: the pinned account answered 11 of 11 requests while
    a 9%-used account answered none.
    """

    def _pool(self, tmp_path, headrooms: list[float | None]) -> AccountManager:
        manager = AccountManager(
            credentials_file=str(tmp_path / "credentials.json"), state_file=str(tmp_path / "state.json")
        )
        for index, headroom in enumerate(headrooms):
            account_id = f"/creds/account{index}.json"
            account = Account(id=account_id)
            account.auth_manager = MagicMock()
            account.models_cached_at = time.time()
            account.quota_headroom = headroom
            manager._accounts[account_id] = account
        return manager

    @pytest.mark.asyncio
    async def test_last_position_account_is_not_starved(self, tmp_path):
        """
        What it does: Pins the legacy sticky index to the last account and draws
                      many times from a pool whose last entry has quota left
        Purpose: Reproduces the live starvation bug. Under the sticky policy the
                 pinned account won every draw; weighted routing must reach the
                 account sitting behind it in insertion order.
        """
        print("\n=== Test: the account behind the sticky cursor still gets traffic ===")

        # Mirrors the live pool: the low-usage account sits at index 0 and the
        # cursor is pinned to the account after it.
        manager = self._pool(tmp_path, headrooms=[0.9, 0.73])
        starved, pinned = "/creds/account0.json", "/creds/account1.json"
        manager._current_account_index = 1

        counts = {starved: 0, pinned: 0}
        for _ in range(400):
            selected = await manager.get_next_account("claude-sonnet-4-5")
            assert selected is not None
            counts[selected.id] += 1

        print(f"Selection counts: {counts}")
        assert counts[starved] > 0
        assert counts[pinned] > 0

    @pytest.mark.asyncio
    async def test_more_headroom_wins_more_traffic(self, tmp_path):
        """
        What it does: Draws repeatedly from a pool with very different headroom
        Purpose: Selection must prefer the account with more quota left, which is
                 what makes the pool drain evenly instead of one account at a time
        """
        print("\n=== Test: more remaining quota earns more traffic ===")

        manager = self._pool(tmp_path, headrooms=[0.9, 0.1])
        rich, poor = "/creds/account0.json", "/creds/account1.json"

        counts = {rich: 0, poor: 0}
        for _ in range(600):
            selected = await manager.get_next_account("claude-sonnet-4-5")
            assert selected is not None
            counts[selected.id] += 1

        print(f"Selection counts: {counts} (weights 0.9 vs 0.1)")
        assert counts[rich] > counts[poor]
        # Both stay reachable: weighting is a preference, not a filter.
        assert counts[poor] > 0

    @pytest.mark.asyncio
    async def test_depleted_account_stays_reachable(self, tmp_path):
        """
        What it does: Gives one account zero headroom and drains draws
        Purpose: A 100%-used reading is not a refusal (overage may be on, the
                 reading may be stale), so the account must keep a low but real
                 chance instead of being excluded by telemetry alone
        """
        print("\n=== Test: a fully-used account is deprioritized, not excluded ===")

        manager = self._pool(tmp_path, headrooms=[0.5, 0.0])
        healthy, depleted = "/creds/account0.json", "/creds/account1.json"

        counts = {healthy: 0, depleted: 0}
        for _ in range(2000):
            selected = await manager.get_next_account("claude-sonnet-4-5")
            assert selected is not None
            counts[selected.id] += 1

        print(f"Selection counts: {counts}")
        assert counts[healthy] > counts[depleted]
        assert counts[depleted] > 0

    @pytest.mark.asyncio
    async def test_unknown_headroom_is_reachable(self, tmp_path):
        """
        What it does: Leaves one account's headroom unset
        Purpose: Usage polling can lag or fail per account; an unpolled account
                 must not become unroutable, which would reintroduce starvation
        """
        print("\n=== Test: an account with no quota reading still routes ===")

        manager = self._pool(tmp_path, headrooms=[0.6, None])
        known, unknown = "/creds/account0.json", "/creds/account1.json"

        counts = {known: 0, unknown: 0}
        for _ in range(400):
            selected = await manager.get_next_account("claude-sonnet-4-5")
            assert selected is not None
            counts[selected.id] += 1

        print(f"Selection counts: {counts}")
        assert counts[unknown] > 0
        assert counts[known] > 0

    @pytest.mark.asyncio
    async def test_health_policy_still_excludes(self, tmp_path):
        """
        What it does: Suspends the highest-weight account
        Purpose: Weighting reorders candidates only; every existing exclusion
                 (suspension, quota quarantine, rate limit, breaker) must still win
        """
        print("\n=== Test: weighting never overrides an exclusion ===")

        manager = self._pool(tmp_path, headrooms=[1.0, 0.05])
        suspended, usable = "/creds/account0.json", "/creds/account1.json"
        manager._accounts[suspended].suspended_until = time.time() + 3600

        for _ in range(50):
            selected = await manager.get_next_account("claude-sonnet-4-5")
            assert selected is not None
            assert selected.id == usable

        print("✓ Suspended account never selected despite the highest weight")

    @pytest.mark.asyncio
    async def test_sticky_policy_can_be_restored(self, tmp_path):
        """
        What it does: Disables ACCOUNT_QUOTA_WEIGHTED_ROUTING and draws twice
        Purpose: The old behavior stays available as a rollback switch, and with
                 it selection is deterministic from the persisted cursor again
        """
        print("\n=== Test: the legacy sticky policy remains selectable ===")

        manager = self._pool(tmp_path, headrooms=[0.1, 0.9])
        manager._current_account_index = 1

        with patch("kiro.account_manager.ACCOUNT_QUOTA_WEIGHTED_ROUTING", False):
            for _ in range(10):
                selected = await manager.get_next_account("claude-sonnet-4-5")
                assert selected is not None
                # Pinned account wins every time despite the lower headroom.
                assert selected.id == "/creds/account1.json"

        print("✓ Sticky rotation restored when the flag is off")

    def test_set_quota_headroom_clamps_and_accepts_none(self, tmp_path):
        """
        What it does: Feeds out-of-range and missing headroom values
        Purpose: Telemetry must never produce a negative or >1 weight, and a
                 missing reading must reset to unknown rather than to zero
        """
        print("\n=== Test: headroom input is clamped ===")

        manager = self._pool(tmp_path, headrooms=[0.5])
        account_id = "/creds/account0.json"

        manager.set_quota_headroom(account_id, 4.2)
        assert manager._accounts[account_id].quota_headroom == 1.0

        manager.set_quota_headroom(account_id, -3.0)
        assert manager._accounts[account_id].quota_headroom == 0.0

        manager.set_quota_headroom(account_id, None)
        assert manager._accounts[account_id].quota_headroom is None

        # An unknown account is ignored rather than raising.
        manager.set_quota_headroom("/creds/missing.json", 0.5)

    def test_headroom_is_not_persisted(self, tmp_path):
        """
        What it does: Inspects the durable state document
        Purpose: A stale headroom misroutes, so the weight is deliberately
                 rebuilt from usage rows instead of being persisted with state
        """
        print("\n=== Test: routing weight stays out of the state document ===")

        manager = self._pool(tmp_path, headrooms=[0.42])
        document = manager._state_document()

        serialized = json.dumps(document)
        print(f"State keys: {sorted(document.keys())}")
        assert "quota_headroom" not in serialized

    @pytest.mark.asyncio
    async def test_zero_configured_floor_does_not_starve(self, tmp_path):
        """
        What it does: Forces both quota floors to 0 and draws from a pool whose
                      accounts all read as fully used
        Purpose: A zero weight cannot be sampled, so equal zero weights would
                 order by pool insertion and starve everything behind the first
                 entry - the exact bug this policy removes. MINIMUM_ROUTING_WEIGHT
                 must keep every account drawable.
        """
        print("\n=== Test: a zero-configured weight floor still rotates ===")

        manager = self._pool(tmp_path, headrooms=[0.0, 0.0, 0.0])

        counts = {f"/creds/account{i}.json": 0 for i in range(3)}
        with (
            patch("kiro.account_manager.ACCOUNT_DEPLETED_QUOTA_WEIGHT", 0.0),
            patch("kiro.account_manager.ACCOUNT_UNKNOWN_QUOTA_WEIGHT", 0.0),
        ):
            for _ in range(300):
                selected = await manager.get_next_account("claude-sonnet-4-5")
                assert selected is not None
                counts[selected.id] += 1

        print(f"Selection counts with zero floors: {counts}")
        assert all(count > 0 for count in counts.values())

    @pytest.mark.asyncio
    async def test_all_unknown_headroom_rotates(self, tmp_path):
        """
        What it does: Draws from a pool where no account has a quota reading
        Purpose: Before the first usage poll every weight is equal; selection
                 must still spread instead of collapsing onto one account
        """
        print("\n=== Test: an unpolled pool still rotates ===")

        manager = self._pool(tmp_path, headrooms=[None, None, None])

        counts = {f"/creds/account{i}.json": 0 for i in range(3)}
        for _ in range(300):
            selected = await manager.get_next_account("claude-sonnet-4-5")
            assert selected is not None
            counts[selected.id] += 1

        print(f"Selection counts with no readings: {counts}")
        assert all(count > 0 for count in counts.values())

    def test_routing_weight_is_always_positive(self, tmp_path):
        """
        What it does: Inspects the weight for depleted and unknown accounts under
                      zeroed configuration
        Purpose: Locks the invariant directly rather than only through sampling
        """
        print("\n=== Test: routing weight never reaches zero ===")

        manager = self._pool(tmp_path, headrooms=[0.0, None])
        depleted = manager._accounts["/creds/account0.json"]
        unknown = manager._accounts["/creds/account1.json"]

        with (
            patch("kiro.account_manager.ACCOUNT_DEPLETED_QUOTA_WEIGHT", 0.0),
            patch("kiro.account_manager.ACCOUNT_UNKNOWN_QUOTA_WEIGHT", 0.0),
        ):
            assert manager._routing_weight(depleted) > 0.0
            assert manager._routing_weight(unknown) > 0.0
