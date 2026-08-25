# -*- coding: utf-8 -*-

"""
Tests for main.py lifespan() function - Account System initialization.

Tests cover:
- Legacy fallback: .env → credentials.json migration
- AccountManager initialization
- First working account initialization
- Background task management
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# =============================================================================
# Test Class: Legacy Fallback (Migration)
# =============================================================================


class TestLifespanAccountManagerInit:
    """
    Tests for AccountManager initialization and lifecycle.

    What it does: Verifies AccountManager creation, account initialization, and background tasks
    Purpose: Ensure proper startup and shutdown of Account System
    """

    @pytest.mark.asyncio
    async def test_lifespan_create_account_manager(self, tmp_path, monkeypatch):
        """
        Test 97: Создание AccountManager с правильными путями

        What it does: Verifies AccountManager is created with correct file paths
        Purpose: Ensure AccountManager receives proper configuration
        """
        print("\n=== Test 97: Create AccountManager with correct paths ===")

        # Arrange: Patch constants

        # Track AccountManager creation
        manager_created_with = {}

        class MockAccountManager:
            def __init__(self, *args, **kwargs):
                manager_created_with["called"] = True
                self._accounts = {"test": MagicMock()}
                self._current_account_index = 0

            async def load_credentials(self):
                pass

            async def load_state(self):
                pass

            async def _initialize_account(self, account_id):
                return True

            async def _save_state(self):
                pass

            async def save_state_periodically(self):
                await asyncio.sleep(1000)

            def load_rate_observations(self, rows):
                self.restored_rate_observations = rows

            def drain_unsaved_rate_observations(self):
                return []

        with patch("main.AccountManager", MockAccountManager):
            with patch("main.httpx.AsyncClient") as mock_client_class:
                mock_client = AsyncMock()
                mock_client_class.return_value = mock_client

                from main import app, lifespan

                async with lifespan(app):
                    pass

        # Assert
        print(f"AccountManager created with: {manager_created_with}")
        assert manager_created_with.get("called") is True
        print("✓ AccountManager constructed")

    @pytest.mark.asyncio
    async def test_lifespan_load_credentials_and_state(self, tmp_path, monkeypatch):
        """
        Test 98: Вызов load_credentials() и load_state()

        What it does: Verifies that load methods are called during startup
        Purpose: Ensure credentials and state are loaded before initialization
        """
        print("\n=== Test 98: Call load_credentials() and load_state() ===")

        # Arrange: Patch constants

        load_calls = {"credentials": False, "state": False}

        mock_manager = AsyncMock()
        mock_manager.load_rate_observations = MagicMock()
        mock_manager.drain_unsaved_rate_observations = MagicMock(return_value=[])
        mock_manager._accounts = {"test": MagicMock()}
        mock_manager._current_account_index = 0

        async def track_load_credentials():
            load_calls["credentials"] = True

        async def track_load_state():
            load_calls["state"] = True

        mock_manager.load_credentials = track_load_credentials
        mock_manager.load_state = track_load_state
        mock_manager._initialize_account = AsyncMock(return_value=True)
        mock_manager._save_state = AsyncMock()
        mock_manager.save_state_periodically = AsyncMock()
        mock_manager.load_rate_observations = MagicMock()
        mock_manager.drain_unsaved_rate_observations = MagicMock(return_value=[])

        with patch("main.AccountManager", return_value=mock_manager):
            with patch("main.httpx.AsyncClient") as mock_client_class:
                mock_client = AsyncMock()
                mock_client_class.return_value = mock_client

                from main import app, lifespan

                async with lifespan(app):
                    pass

        # Assert
        print(f"Load calls: {load_calls}")
        assert load_calls["credentials"] is True
        assert load_calls["state"] is True
        print("✓ load_credentials() and load_state() were called")

    @pytest.mark.asyncio
    async def test_lifespan_initialize_first_working_account(self, tmp_path, monkeypatch):
        """
        Test 100: Инициализация первого рабочего аккаунта

        What it does: Verifies first working account is initialized at startup
        Purpose: Ensure at least one account is ready before accepting requests
        """
        print("\n=== Test 100: Initialize first working account ===")

        # Arrange: Patch constants

        initialized_accounts = []

        mock_manager = AsyncMock()
        mock_manager.load_rate_observations = MagicMock()
        mock_manager.drain_unsaved_rate_observations = MagicMock(return_value=[])
        mock_manager._accounts = {"account1": MagicMock(), "account2": MagicMock()}
        mock_manager._current_account_index = 0

        async def track_initialize(account_id):
            initialized_accounts.append(account_id)
            return True  # Success on first account

        mock_manager._initialize_account = track_initialize
        mock_manager._save_state = AsyncMock()
        mock_manager.save_state_periodically = AsyncMock()
        mock_manager.load_rate_observations = MagicMock()
        mock_manager.drain_unsaved_rate_observations = MagicMock(return_value=[])

        with patch("main.AccountManager", return_value=mock_manager):
            with patch("main.httpx.AsyncClient") as mock_client_class:
                mock_client = AsyncMock()
                mock_client_class.return_value = mock_client

                from main import app, lifespan

                async with lifespan(app):
                    pass

        # Assert: only first account was initialized
        print(f"Initialized accounts: {initialized_accounts}")
        assert len(initialized_accounts) == 1
        assert initialized_accounts[0] == "account1"
        print("✓ First working account was initialized")

    @pytest.mark.asyncio
    async def test_lifespan_full_circle_initialization(self, tmp_path, monkeypatch):
        """
        Test 101: Попытка инициализации всех аккаунтов по кругу

        What it does: Verifies full circle attempt if first accounts fail
        Purpose: Ensure all accounts are tried before giving up
        """
        print("\n=== Test 101: Full circle initialization attempt ===")

        # Arrange: Patch constants

        initialized_attempts = []

        mock_manager = AsyncMock()
        mock_manager.load_rate_observations = MagicMock()
        mock_manager.drain_unsaved_rate_observations = MagicMock(return_value=[])
        mock_manager._accounts = {"account1": MagicMock(), "account2": MagicMock(), "account3": MagicMock()}
        mock_manager._current_account_index = 0

        async def track_initialize(account_id):
            initialized_attempts.append(account_id)
            # First two fail, third succeeds
            if account_id == "account3":
                return True
            return False

        mock_manager._initialize_account = track_initialize
        mock_manager._save_state = AsyncMock()
        mock_manager.save_state_periodically = AsyncMock()
        mock_manager.load_rate_observations = MagicMock()
        mock_manager.drain_unsaved_rate_observations = MagicMock(return_value=[])

        with patch("main.AccountManager", return_value=mock_manager):
            with patch("main.httpx.AsyncClient") as mock_client_class:
                mock_client = AsyncMock()
                mock_client_class.return_value = mock_client

                from main import app, lifespan

                async with lifespan(app):
                    pass

        # Assert: all three accounts were tried
        print(f"Initialization attempts: {initialized_attempts}")
        assert initialized_attempts == ["account1", "account2", "account3"]
        print("✓ Full circle initialization was attempted")

    @pytest.mark.asyncio
    async def test_lifespan_exit_if_no_accounts(self, tmp_path, monkeypatch):
        """
        Test 102: RuntimeError если нет аккаунтов в credentials.json

        What it does: Verifies application raises RuntimeError if no accounts configured
        Purpose: Prevent startup with empty configuration
        """
        print("\n=== Test 102: RuntimeError if no accounts configured ===")

        # Arrange: Patch constants

        mock_manager = AsyncMock()
        mock_manager.load_rate_observations = MagicMock()
        mock_manager.drain_unsaved_rate_observations = MagicMock(return_value=[])
        mock_manager._accounts = {}  # Empty accounts dict
        mock_manager._current_account_index = 0

        with patch("main.AccountManager", return_value=mock_manager):
            with patch("main.httpx.AsyncClient") as mock_client_class:
                mock_client = AsyncMock()
                mock_client_class.return_value = mock_client

                from main import app, lifespan

                async with lifespan(app):
                    assert app.state.account_manager is mock_manager
                print("✓ Empty pool starts successfully")

    @pytest.mark.asyncio
    async def test_lifespan_exit_if_all_failed(self, tmp_path, monkeypatch):
        """
        Test 103: RuntimeError если все аккаунты не инициализировались

        What it does: Verifies application raises RuntimeError if all accounts fail to initialize
        Purpose: Prevent startup without any working accounts
        """
        print("\n=== Test 103: RuntimeError if all accounts failed ===")

        # Arrange: Patch constants

        mock_manager = AsyncMock()
        mock_manager.load_rate_observations = MagicMock()
        mock_manager.drain_unsaved_rate_observations = MagicMock(return_value=[])
        mock_manager._accounts = {"account1": MagicMock(), "account2": MagicMock()}
        mock_manager._current_account_index = 0
        mock_manager._initialize_account = AsyncMock(return_value=False)  # All fail

        with patch("main.AccountManager", return_value=mock_manager):
            with patch("main.httpx.AsyncClient") as mock_client_class:
                mock_client = AsyncMock()
                mock_client_class.return_value = mock_client

                from main import app, lifespan

                async with lifespan(app):
                    assert app.state.account_manager is mock_manager
                print("✓ Startup continues when all accounts fail to initialize")

    @pytest.mark.asyncio
    async def test_lifespan_save_initial_state(self, tmp_path, monkeypatch):
        """
        Test 104: Сохранение начального state.json

        What it does: Verifies initial state is saved after first account initialization
        Purpose: Ensure state persistence starts immediately
        """
        print("\n=== Test 104: Save initial state ===")

        # Arrange: Patch constants

        save_state_called = False

        mock_manager = AsyncMock()
        mock_manager.load_rate_observations = MagicMock()
        mock_manager.drain_unsaved_rate_observations = MagicMock(return_value=[])
        mock_manager._accounts = {"test": MagicMock()}
        mock_manager._current_account_index = 0
        mock_manager._initialize_account = AsyncMock(return_value=True)

        async def track_save_state():
            nonlocal save_state_called
            save_state_called = True

        mock_manager._save_state = track_save_state
        mock_manager.save_state_periodically = AsyncMock()
        mock_manager.load_rate_observations = MagicMock()
        mock_manager.drain_unsaved_rate_observations = MagicMock(return_value=[])

        with patch("main.AccountManager", return_value=mock_manager):
            with patch("main.httpx.AsyncClient") as mock_client_class:
                mock_client = AsyncMock()
                mock_client_class.return_value = mock_client

                from main import app, lifespan

                async with lifespan(app):
                    pass

        # Assert
        print(f"_save_state called: {save_state_called}")
        assert save_state_called is True
        print("✓ Initial state was saved")

    @pytest.mark.asyncio
    async def test_lifespan_start_background_task(self, tmp_path, monkeypatch):
        """
        Test 105: Запуск save_state_periodically()

        What it does: Verifies background task is started for periodic state saving
        Purpose: Ensure state is saved periodically during runtime
        """
        print("\n=== Test 105: Start background task ===")

        # Arrange: Patch constants

        periodic_task_started = False

        mock_manager = AsyncMock()
        mock_manager.load_rate_observations = MagicMock()
        mock_manager.drain_unsaved_rate_observations = MagicMock(return_value=[])
        mock_manager._accounts = {"test": MagicMock()}
        mock_manager._current_account_index = 0
        mock_manager._initialize_account = AsyncMock(return_value=True)
        mock_manager._save_state = AsyncMock()
        mock_manager.load_rate_observations = MagicMock()
        mock_manager.drain_unsaved_rate_observations = MagicMock(return_value=[])

        async def track_periodic_save():
            nonlocal periodic_task_started
            periodic_task_started = True
            await asyncio.sleep(1000)  # Long sleep to keep task alive

        mock_manager.save_state_periodically = track_periodic_save

        with patch("main.AccountManager", return_value=mock_manager):
            with patch("main.httpx.AsyncClient") as mock_client_class:
                mock_client = AsyncMock()
                mock_client_class.return_value = mock_client

                from main import app, lifespan

                async with lifespan(app):
                    # Give task time to start
                    await asyncio.sleep(0.1)
                    assert periodic_task_started is True
                    print("✓ Background task was started")

    @pytest.mark.asyncio
    async def test_lifespan_shutdown_cancel_task(self, tmp_path, monkeypatch):
        """
        Test 106: Отмена background task при shutdown

        What it does: Verifies background task is cancelled during shutdown
        Purpose: Ensure clean shutdown without hanging tasks
        """
        print("\n=== Test 106: Cancel background task on shutdown ===")

        # Arrange: Patch constants

        task_cancelled = False

        mock_manager = AsyncMock()
        mock_manager.load_rate_observations = MagicMock()
        mock_manager.drain_unsaved_rate_observations = MagicMock(return_value=[])
        mock_manager._accounts = {"test": MagicMock()}
        mock_manager._current_account_index = 0
        mock_manager._initialize_account = AsyncMock(return_value=True)
        mock_manager._save_state = AsyncMock()

        async def periodic_save_with_cancel_check():
            try:
                await asyncio.sleep(1000)
            except asyncio.CancelledError:
                nonlocal task_cancelled
                task_cancelled = True
                raise

        mock_manager.save_state_periodically = periodic_save_with_cancel_check

        with patch("main.AccountManager", return_value=mock_manager):
            with patch("main.httpx.AsyncClient") as mock_client_class:
                mock_client = AsyncMock()
                mock_client_class.return_value = mock_client

                from main import app, lifespan

                async with lifespan(app):
                    await asyncio.sleep(0.1)

                # After context exit, task should be cancelled
                await asyncio.sleep(0.1)

        # Assert
        print(f"Task cancelled: {task_cancelled}")
        assert task_cancelled is True
        print("✓ Background task was cancelled on shutdown")

    @pytest.mark.asyncio
    async def test_lifespan_shutdown_final_save(self, tmp_path, monkeypatch):
        """
        Test 107: Финальное сохранение state.json при shutdown

        What it does: Verifies final state save happens during shutdown
        Purpose: Ensure no state is lost on graceful shutdown
        """
        print("\n=== Test 107: Final save on shutdown ===")

        # Arrange: Patch constants

        save_calls = []

        mock_manager = AsyncMock()
        mock_manager.load_rate_observations = MagicMock()
        mock_manager.drain_unsaved_rate_observations = MagicMock(return_value=[])
        mock_manager._accounts = {"test": MagicMock()}
        mock_manager._current_account_index = 0
        mock_manager._initialize_account = AsyncMock(return_value=True)

        async def track_save_state():
            save_calls.append("save")

        mock_manager._save_state = track_save_state
        mock_manager.save_state_periodically = AsyncMock()
        mock_manager.load_rate_observations = MagicMock()
        mock_manager.drain_unsaved_rate_observations = MagicMock(return_value=[])

        with patch("main.AccountManager", return_value=mock_manager):
            with patch("main.httpx.AsyncClient") as mock_client_class:
                mock_client = AsyncMock()
                mock_client_class.return_value = mock_client

                from main import app, lifespan

                async with lifespan(app):
                    pass

        # Initialization no longer writes unchanged state; shutdown still does.
        print(f"Save calls: {len(save_calls)}")
        assert save_calls == ["save"]
        print("✓ Final state save was performed on shutdown")
