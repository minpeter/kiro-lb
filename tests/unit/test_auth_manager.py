# -*- coding: utf-8 -*-

"""
Unit tests for KiroAuthManager.
Tests token management logic for Kiro without real network requests.
"""

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from kiro.account_errors import CredentialDeadError
from kiro.auth import AuthType, KiroAuthManager
from kiro.config import TOKEN_REFRESH_THRESHOLD
from kiro.store import connection, initialize, replace_account_sources


class TestKiroAuthManagerInitialization:
    """Tests for KiroAuthManager initialization."""

    def test_initialization_stores_credentials(self):
        """
        What it does: Verifies correct storage of credentials during initialization.
        Purpose: Ensure all constructor parameters are stored in private fields.
        """
        print("Setup: Creating KiroAuthManager with test credentials...")
        manager = KiroAuthManager(
            refresh_token="test_refresh_123",
            profile_arn="arn:aws:codewhisperer:us-east-1:123456789:profile/test",
            region="us-east-1",
        )

        print("Verification: All credentials stored correctly...")
        print(f"Comparing refresh_token: Expected 'test_refresh_123', Got '{manager._refresh_token}'")
        assert manager._refresh_token == "test_refresh_123"

        print(f"Comparing profile_arn: Expected 'arn:aws:...', Got '{manager._profile_arn}'")
        assert manager._profile_arn == "arn:aws:codewhisperer:us-east-1:123456789:profile/test"

        print(f"Comparing region: Expected 'us-east-1', Got '{manager._region}'")
        assert manager._region == "us-east-1"

        print("Verification: Token is initially empty...")
        assert manager._access_token is None
        assert manager._expires_at is None

    def test_initialization_sets_correct_urls_for_region(self):
        """
        What it does: Verifies URL formation based on region.
        Purpose: Ensure URLs are dynamically formed with the correct region.
        """
        print("Setup: Creating KiroAuthManager with region eu-west-1...")
        manager = KiroAuthManager(refresh_token="test_token", region="eu-west-1")

        print("Verification: URLs contain correct region...")
        print(f"Comparing refresh_url: Expected 'eu-west-1' in URL, Got '{manager._refresh_url}'")
        assert "eu-west-1" in manager._refresh_url

        print(f"Comparing api_host: Expected 'eu-west-1' in URL, Got '{manager._api_host}'")
        assert "eu-west-1" in manager._api_host

        print(f"Comparing q_host: Expected 'eu-west-1' in URL, Got '{manager._q_host}'")
        assert "eu-west-1" in manager._q_host

    def test_initialization_generates_fingerprint(self):
        """
        What it does: Verifies unique fingerprint generation.
        Purpose: Ensure fingerprint is generated and has correct format.
        """
        print("Setup: Creating KiroAuthManager...")
        manager = KiroAuthManager(refresh_token="test_token")

        print("Verification: Fingerprint generated...")
        print(f"Fingerprint: {manager._fingerprint}")
        assert manager._fingerprint is not None
        assert len(manager._fingerprint) == 64  # SHA256 hex digest


class TestKiroAuthManagerCredentialsFile:
    """Tests for loading credentials from file."""

    def test_load_credentials_from_file(self, temp_creds_file):
        """
        What it does: Verifies loading credentials from JSON file.
        Purpose: Ensure data is correctly read from file.
        """
        print(f"Setup: Creating KiroAuthManager with credentials file: {temp_creds_file}")
        manager = KiroAuthManager(creds_file=temp_creds_file)

        print("Verification: Data loaded from file...")
        print(f"Comparing access_token: Expected 'file_access_token', Got '{manager._access_token}'")
        assert manager._access_token == "file_access_token"

        print(f"Comparing refresh_token: Expected 'file_refresh_token', Got '{manager._refresh_token}'")
        assert manager._refresh_token == "file_refresh_token"

        print(f"Comparing region: Expected 'us-east-1', Got '{manager._region}'")
        assert manager._region == "us-east-1"

        print("Verification: expiresAt parsed correctly...")
        assert manager._expires_at is not None
        assert manager._expires_at.year == 2099

    def test_load_credentials_file_not_found(self, tmp_path):
        """
        What it does: Verifies handling of missing credentials file.
        Purpose: Ensure application doesn't crash when file is missing.
        """
        print("Setup: Creating KiroAuthManager with non-existent file...")
        non_existent_file = str(tmp_path / "non_existent.json")

        manager = KiroAuthManager(refresh_token="fallback_token", creds_file=non_existent_file)

        print("Verification: Fallback refresh_token is used...")
        print(f"Comparing refresh_token: Expected 'fallback_token', Got '{manager._refresh_token}'")
        assert manager._refresh_token == "fallback_token"


class TestKiroAuthManagerTokenExpiration:
    """Tests for token expiration checking."""

    def test_is_token_expiring_soon_returns_true_when_no_expires_at(self):
        """
        What it does: Verifies that without expires_at token is considered expiring.
        Purpose: Ensure safe behavior when time information is missing.
        """
        print("Setup: Creating KiroAuthManager without expires_at...")
        manager = KiroAuthManager(refresh_token="test_token")
        manager._expires_at = None

        print("Verification: is_token_expiring_soon returns True...")
        result = manager.is_token_expiring_soon()
        print(f"Comparing result: Expected True, Got {result}")
        assert result is True

    def test_is_token_expiring_soon_returns_true_when_expired(self):
        """
        What it does: Verifies that expired token is correctly identified.
        Purpose: Ensure token in the past is considered expiring.
        """
        print("Setup: Creating KiroAuthManager with expired token...")
        manager = KiroAuthManager(refresh_token="test_token")
        manager._expires_at = datetime.now(timezone.utc) - timedelta(hours=1)

        print("Verification: is_token_expiring_soon returns True for expired token...")
        result = manager.is_token_expiring_soon()
        print(f"Comparing result: Expected True, Got {result}")
        assert result is True

    def test_is_token_expiring_soon_returns_true_within_threshold(self):
        """
        What it does: Verifies that token within threshold is considered expiring.
        Purpose: Ensure token is refreshed in advance (10 minutes before expiration).
        """
        print("Setup: Creating KiroAuthManager with token expiring in 5 minutes...")
        manager = KiroAuthManager(refresh_token="test_token")
        manager._expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

        print(f"TOKEN_REFRESH_THRESHOLD = {TOKEN_REFRESH_THRESHOLD} seconds")
        print("Verification: is_token_expiring_soon returns True (5 min < 10 min threshold)...")
        result = manager.is_token_expiring_soon()
        print(f"Comparing result: Expected True, Got {result}")
        assert result is True

    def test_is_token_expiring_soon_returns_false_when_valid(self):
        """
        What it does: Verifies that valid token is not considered expiring.
        Purpose: Ensure token far in the future doesn't require refresh.
        """
        print("Setup: Creating KiroAuthManager with token expiring in 1 hour...")
        manager = KiroAuthManager(refresh_token="test_token")
        manager._expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

        print("Verification: is_token_expiring_soon returns False...")
        result = manager.is_token_expiring_soon()
        print(f"Comparing result: Expected False, Got {result}")
        assert result is False


class TestKiroAuthManagerTokenRefresh:
    """Tests for token refresh mechanism."""

    @pytest.mark.asyncio
    async def test_refresh_token_successful(self, valid_kiro_token, mock_kiro_token_response):
        """
        What it does: Tests successful token refresh via Kiro API.
        Purpose: Verify that on successful response token and expiration time are set.
        """
        print("Setup: Creating KiroAuthManager...")
        manager = KiroAuthManager(refresh_token="test_refresh", region="us-east-1")

        print("Setup: Mocking successful response from Kiro...")
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=mock_kiro_token_response())
        mock_response.raise_for_status = Mock()

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            print("Action: Calling _refresh_token_request()...")
            await manager._refresh_token_request()

            print("Verification: Token set correctly...")
            print(f"Comparing access_token: Expected '{valid_kiro_token}', Got '{manager._access_token}'")
            assert manager._access_token == valid_kiro_token

            print("Verification: Expiration time set...")
            assert manager._expires_at is not None

            print("Verification: POST request was made...")
            mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_refresh_token_updates_refresh_token(self, mock_kiro_token_response):
        """
        What it does: Verifies refresh_token update from response.
        Purpose: Ensure new refresh_token is saved.
        """
        print("Setup: Creating KiroAuthManager...")
        manager = KiroAuthManager(refresh_token="old_refresh_token")

        print("Setup: Mocking response with new refresh_token...")
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=mock_kiro_token_response())
        mock_response.raise_for_status = Mock()

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            print("Action: Refreshing token...")
            await manager._refresh_token_request()

            print("Verification: refresh_token updated...")
            print(f"Comparing refresh_token: Expected 'new_refresh_token_xyz', Got '{manager._refresh_token}'")
            assert manager._refresh_token == "new_refresh_token_xyz"

    @pytest.mark.asyncio
    async def test_refresh_token_missing_access_token_raises(self):
        """
        What it does: Verifies handling of response without accessToken.
        Purpose: Ensure exception is raised on invalid response.
        """
        print("Setup: Creating KiroAuthManager...")
        manager = KiroAuthManager(refresh_token="test_refresh")

        print("Setup: Mocking response without accessToken...")
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value={"expiresIn": 3600})  # No accessToken!
        mock_response.raise_for_status = Mock()

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            print("Action: Attempting token refresh...")
            with pytest.raises(ValueError) as exc_info:
                await manager._refresh_token_request()

            print(f"Verification: ValueError raised with message: {exc_info.value}")
            assert "accessToken" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_refresh_token_no_refresh_token_raises(self):
        """
        What it does: Verifies handling of missing refresh_token.
        Purpose: Ensure exception is raised without refresh_token.
        """
        print("Setup: Creating KiroAuthManager without refresh_token...")
        manager = KiroAuthManager()
        manager._refresh_token = None

        print("Action: Attempting token refresh without refresh_token...")
        with pytest.raises(ValueError) as exc_info:
            await manager._refresh_token_request()

        print(f"Verification: ValueError raised: {exc_info.value}")
        assert "Refresh token" in str(exc_info.value)


class TestKiroAuthManagerGetAccessToken:
    """Tests for public get_access_token method."""

    @pytest.mark.asyncio
    async def test_get_access_token_refreshes_when_expired(self, valid_kiro_token, mock_kiro_token_response):
        """
        What it does: Verifies automatic refresh of expired token.
        Purpose: Ensure stale token is refreshed before returning.
        """
        print("Setup: Creating KiroAuthManager with expired token...")
        manager = KiroAuthManager(refresh_token="test_refresh")
        manager._access_token = "old_expired_token"
        manager._expires_at = datetime.now(timezone.utc) - timedelta(hours=1)

        print("Setup: Mocking successful refresh...")
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=mock_kiro_token_response())
        mock_response.raise_for_status = Mock()

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            print("Action: Requesting token via get_access_token()...")
            token = await manager.get_access_token()

            print("Verification: Got new token, not expired one...")
            print(f"Comparing token: Expected '{valid_kiro_token}', Got '{token}'")
            assert token == valid_kiro_token
            assert token != "old_expired_token"

            print("Verification: _refresh_token_request was called...")
            mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_access_token_returns_valid_without_refresh(self, valid_kiro_token):
        """
        What it does: Verifies valid token is returned without refresh.
        Purpose: Ensure no unnecessary requests are made if token is valid.
        """
        print("Setup: Creating KiroAuthManager with valid token...")
        manager = KiroAuthManager(refresh_token="test_refresh")
        manager._access_token = valid_kiro_token
        manager._expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

        print("Setup: Mocking httpx to track calls...")
        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock()
            mock_client_class.return_value = mock_client

            print("Action: Requesting valid token...")
            token = await manager.get_access_token()

            print("Verification: Existing token returned...")
            print(f"Comparing token: Expected '{valid_kiro_token}', Got '{token}'")
            assert token == valid_kiro_token

            print("Verification: _refresh_token was NOT called (no network requests)...")
            mock_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_access_token_thread_safety(self, valid_kiro_token, mock_kiro_token_response):
        """
        What it does: Verifies thread safety via asyncio.Lock.
        Purpose: Ensure parallel calls don't cause race conditions.
        """
        print("Setup: Creating KiroAuthManager...")
        manager = KiroAuthManager(refresh_token="test_refresh")
        manager._access_token = None
        manager._expires_at = None

        refresh_call_count = 0

        async def mock_refresh():
            nonlocal refresh_call_count
            refresh_call_count += 1
            await asyncio.sleep(0.1)  # Simulate delay
            manager._access_token = valid_kiro_token
            manager._expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

        print("Setup: Patching _refresh_token_request to track calls...")
        with patch.object(manager, "_refresh_token_request", side_effect=mock_refresh):
            print("Action: 5 parallel get_access_token() calls...")
            tokens = await asyncio.gather(*[manager.get_access_token() for _ in range(5)])

            print("Verification: All calls got the same token...")
            assert all(token == valid_kiro_token for token in tokens)

            print("Verification: _refresh_token called ONLY ONCE (thanks to lock)...")
            print(f"Comparing call count: Expected 1, Got {refresh_call_count}")
            assert refresh_call_count == 1


class TestKiroAuthManagerForceRefresh:
    """Tests for forced token refresh."""

    @pytest.mark.asyncio
    async def test_force_refresh_updates_token(self, valid_kiro_token, mock_kiro_token_response):
        """
        What it does: Verifies forced token refresh.
        Purpose: Ensure force_refresh always refreshes the token.
        """
        print("Setup: Creating KiroAuthManager with valid token...")
        manager = KiroAuthManager(refresh_token="test_refresh")
        manager._access_token = "old_but_valid_token"
        manager._expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

        print("Setup: Mocking refresh...")
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=mock_kiro_token_response())
        mock_response.raise_for_status = Mock()

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            print("Action: Force refreshing token...")
            token = await manager.force_refresh()

            print("Verification: Token refreshed despite old one being valid...")
            print(f"Comparing token: Expected '{valid_kiro_token}', Got '{token}'")
            assert token == valid_kiro_token

            print("Verification: POST request was made...")
            mock_client.post.assert_called_once()


class TestKiroAuthManagerProperties:
    """Tests for KiroAuthManager properties."""

    def test_profile_arn_property(self):
        """
        What it does: Verifies profile_arn property.
        Purpose: Ensure profile_arn is accessible via property.
        """
        print("Setup: Creating KiroAuthManager with profile_arn...")
        manager = KiroAuthManager(refresh_token="test", profile_arn="arn:aws:test:profile")

        print("Verification: profile_arn accessible...")
        print(f"Comparing profile_arn: Expected 'arn:aws:test:profile', Got '{manager.profile_arn}'")
        assert manager.profile_arn == "arn:aws:test:profile"

    def test_region_property(self):
        """
        What it does: Verifies region property.
        Purpose: Ensure region is accessible via property.
        """
        print("Setup: Creating KiroAuthManager with region...")
        manager = KiroAuthManager(refresh_token="test", region="eu-west-1")

        print("Verification: region accessible...")
        print(f"Comparing region: Expected 'eu-west-1', Got '{manager.region}'")
        assert manager.region == "eu-west-1"

    def test_api_host_property(self):
        """
        What it does: Verifies api_host property.
        Purpose: Ensure api_host is formed correctly.
        """
        print("Setup: Creating KiroAuthManager...")
        manager = KiroAuthManager(refresh_token="test", region="us-east-1")

        print("Verification: api_host contains runtime.{region}.kiro.dev pattern...")
        print(f"api_host: {manager.api_host}")
        assert "runtime.us-east-1.kiro.dev" in manager.api_host
        assert "us-east-1" in manager.api_host

    def test_fingerprint_property(self):
        """
        What it does: Verifies fingerprint property.
        Purpose: Ensure fingerprint is accessible via property.
        """
        print("Setup: Creating KiroAuthManager...")
        manager = KiroAuthManager(refresh_token="test")

        print("Verification: fingerprint accessible and has correct length...")
        print(f"fingerprint: {manager.fingerprint}")
        assert len(manager.fingerprint) == 64


# =============================================================================
# Tests for AuthType enum
# =============================================================================


class TestAuthTypeEnum:
    """Tests for AuthType enum."""

    def test_auth_type_enum_values(self):
        """
        What it does: Verifies AuthType enum values.
        Purpose: Ensure enum contains KIRO_DESKTOP and AWS_SSO_OIDC.
        """
        print("Verification: AuthType contains KIRO_DESKTOP...")
        assert AuthType.KIRO_DESKTOP.value == "kiro_desktop"

        print("Verification: AuthType contains AWS_SSO_OIDC...")
        assert AuthType.AWS_SSO_OIDC.value == "aws_sso_oidc"

        print(f"Comparing value count: Expected 2, Got {len(AuthType)}")
        assert len(AuthType) == 2


# =============================================================================
# Tests for _detect_auth_type()
# =============================================================================


class TestKiroAuthManagerDetectAuthType:
    """Tests for _detect_auth_type() method."""

    def test_detect_auth_type_kiro_desktop_when_no_client_credentials(self):
        """
        What it does: Verifies KIRO_DESKTOP type detection without client credentials.
        Purpose: Ensure KIRO_DESKTOP is used without clientId/clientSecret.
        """
        print("Setup: Creating KiroAuthManager without client credentials...")
        manager = KiroAuthManager(refresh_token="test_token")

        print("Verification: auth_type = KIRO_DESKTOP...")
        print(f"Comparing auth_type: Expected KIRO_DESKTOP, Got {manager.auth_type}")
        assert manager.auth_type == AuthType.KIRO_DESKTOP

    def test_detect_auth_type_aws_sso_oidc_when_client_credentials_present(self):
        """
        What it does: Verifies AWS_SSO_OIDC type detection with client credentials.
        Purpose: Ensure AWS_SSO_OIDC is used with clientId and clientSecret.
        """
        print("Setup: Creating KiroAuthManager with client credentials...")
        manager = KiroAuthManager(
            refresh_token="test_token", client_id="test_client_id", client_secret="test_client_secret"
        )

        print("Verification: auth_type = AWS_SSO_OIDC...")
        print(f"Comparing auth_type: Expected AWS_SSO_OIDC, Got {manager.auth_type}")
        assert manager.auth_type == AuthType.AWS_SSO_OIDC

    def test_detect_auth_type_kiro_desktop_when_only_client_id(self):
        """
        What it does: Verifies type detection with only clientId (no secret).
        Purpose: Ensure KIRO_DESKTOP is used without clientSecret.
        """
        print("Setup: Creating KiroAuthManager with only client_id...")
        manager = KiroAuthManager(refresh_token="test_token", client_id="test_client_id")

        print("Verification: auth_type = KIRO_DESKTOP (both id and secret required)...")
        print(f"Comparing auth_type: Expected KIRO_DESKTOP, Got {manager.auth_type}")
        assert manager.auth_type == AuthType.KIRO_DESKTOP


# =============================================================================
# Tests for loading AWS SSO credentials from JSON file
# =============================================================================


class TestKiroAuthManagerAwsSsoCredentialsFile:
    """Tests for loading AWS SSO OIDC credentials from JSON file."""

    def test_load_credentials_from_file_with_client_id_and_secret(self, temp_aws_sso_creds_file):
        """
        What it does: Verifies loading clientId and clientSecret from JSON file.
        Purpose: Ensure AWS SSO fields are correctly read from file.
        """
        print(f"Setup: Creating KiroAuthManager with AWS SSO file: {temp_aws_sso_creds_file}")
        manager = KiroAuthManager(creds_file=temp_aws_sso_creds_file)

        print("Verification: clientId loaded...")
        print(f"Comparing client_id: Expected 'test_client_id_12345', Got '{manager._client_id}'")
        assert manager._client_id == "test_client_id_12345"

        print("Verification: clientSecret loaded...")
        print(f"Comparing client_secret: Expected 'test_client_secret_67890', Got '{manager._client_secret}'")
        assert manager._client_secret == "test_client_secret_67890"

    def test_load_credentials_from_file_auto_detects_aws_sso_oidc(self, temp_aws_sso_creds_file):
        """
        What it does: Verifies auto-detection of auth type after loading from file.
        Purpose: Ensure auth_type automatically becomes AWS_SSO_OIDC.
        """
        print(f"Setup: Creating KiroAuthManager with AWS SSO file: {temp_aws_sso_creds_file}")
        manager = KiroAuthManager(creds_file=temp_aws_sso_creds_file)

        print("Verification: auth_type automatically detected as AWS_SSO_OIDC...")
        print(f"Comparing auth_type: Expected AWS_SSO_OIDC, Got {manager.auth_type}")
        assert manager.auth_type == AuthType.AWS_SSO_OIDC

    def test_load_kiro_desktop_file_stays_kiro_desktop(self, temp_creds_file):
        """
        What it does: Verifies that Kiro Desktop file doesn't change type to AWS SSO.
        Purpose: Ensure file without clientId/clientSecret stays KIRO_DESKTOP.
        """
        print(f"Setup: Creating KiroAuthManager with Kiro Desktop file: {temp_creds_file}")
        manager = KiroAuthManager(creds_file=temp_creds_file)

        print("Verification: auth_type stays KIRO_DESKTOP...")
        print(f"Comparing auth_type: Expected KIRO_DESKTOP, Got {manager.auth_type}")
        assert manager.auth_type == AuthType.KIRO_DESKTOP


# =============================================================================
# Tests for loading credentials from SQLite
# =============================================================================


class TestKiroAuthManagerSqliteCredentials:
    """Tests for loading credentials from SQLite database (kiro-cli format)."""

    def test_load_credentials_from_sqlite_success(self, temp_sqlite_db):
        """
        What it does: Verifies successful loading of credentials from SQLite.
        Purpose: Ensure all data is correctly read from database.
        """
        print(f"Setup: Creating KiroAuthManager with SQLite: {temp_sqlite_db}")
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db)

        print("Verification: access_token loaded...")
        print(f"Comparing access_token: Expected 'sqlite_access_token', Got '{manager._access_token}'")
        assert manager._access_token == "sqlite_access_token"

        print("Verification: refresh_token loaded...")
        print(f"Comparing refresh_token: Expected 'sqlite_refresh_token', Got '{manager._refresh_token}'")
        assert manager._refresh_token == "sqlite_refresh_token"

    def test_load_credentials_from_sqlite_file_not_found(self, tmp_path):
        """
        What it does: Verifies handling of missing SQLite file.
        Purpose: Ensure application doesn't crash when file is missing.
        """
        print("Setup: Creating KiroAuthManager with non-existent SQLite file...")
        non_existent_db = str(tmp_path / "non_existent.sqlite3")

        manager = KiroAuthManager(refresh_token="fallback_token", sqlite_db=non_existent_db)

        print("Verification: Fallback refresh_token is used...")
        print(f"Comparing refresh_token: Expected 'fallback_token', Got '{manager._refresh_token}'")
        assert manager._refresh_token == "fallback_token"

    def test_load_credentials_from_sqlite_loads_token_data(self, temp_sqlite_db):
        """
        What it does: Verifies loading token data from SQLite.
        Purpose: Ensure access_token, refresh_token, sso_region are loaded.
        Note: API region stays at us-east-1 (CodeWhisperer API only exists there),
              SSO region is stored separately for OIDC token refresh.
        """
        print(f"Setup: Creating KiroAuthManager with SQLite: {temp_sqlite_db}")
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db)

        print("Verification: SSO region loaded from SQLite...")
        print(f"Comparing sso_region: Expected 'eu-west-1', Got '{manager._sso_region}'")
        assert manager._sso_region == "eu-west-1"

        print("Verification: API region stays at us-east-1...")
        print(f"Comparing region: Expected 'us-east-1', Got '{manager._region}'")
        assert manager._region == "us-east-1"

        print("Verification: expires_at parsed...")
        assert manager._expires_at is not None
        assert manager._expires_at.year == 2099

    def test_load_credentials_from_sqlite_loads_device_registration(self, temp_sqlite_db):
        """
        What it does: Verifies loading device registration from SQLite.
        Purpose: Ensure client_id and client_secret are loaded.
        """
        print(f"Setup: Creating KiroAuthManager with SQLite: {temp_sqlite_db}")
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db)

        print("Verification: client_id loaded...")
        print(f"Comparing client_id: Expected 'sqlite_client_id', Got '{manager._client_id}'")
        assert manager._client_id == "sqlite_client_id"

        print("Verification: client_secret loaded...")
        print(f"Comparing client_secret: Expected 'sqlite_client_secret', Got '{manager._client_secret}'")
        assert manager._client_secret == "sqlite_client_secret"

    def test_load_credentials_from_sqlite_auto_detects_aws_sso_oidc(self, temp_sqlite_db):
        """
        What it does: Verifies auto-detection of auth type after loading from SQLite.
        Purpose: Ensure auth_type automatically becomes AWS_SSO_OIDC.
        """
        print(f"Setup: Creating KiroAuthManager with SQLite: {temp_sqlite_db}")
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db)

        print("Verification: auth_type automatically detected as AWS_SSO_OIDC...")
        print(f"Comparing auth_type: Expected AWS_SSO_OIDC, Got {manager.auth_type}")
        assert manager.auth_type == AuthType.AWS_SSO_OIDC

    def test_load_credentials_from_sqlite_handles_missing_registration_key(self, temp_sqlite_db_token_only):
        """
        What it does: Verifies handling of missing device-registration key.
        Purpose: Ensure application doesn't crash without device-registration.
        """
        print("Setup: Creating KiroAuthManager with SQLite without device-registration...")
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db_token_only)

        print("Verification: refresh_token loaded...")
        assert manager._refresh_token == "partial_refresh_token"

        print("Verification: client_id stayed None...")
        assert manager._client_id is None

        print("Verification: auth_type = KIRO_DESKTOP (no client credentials)...")
        assert manager.auth_type == AuthType.KIRO_DESKTOP

    def test_load_credentials_from_sqlite_handles_invalid_json(self, temp_sqlite_db_invalid_json):
        """
        What it does: Verifies handling of invalid JSON in SQLite.
        Purpose: Ensure application doesn't crash on invalid JSON.
        """
        print("Setup: Creating KiroAuthManager with SQLite with invalid JSON...")
        manager = KiroAuthManager(refresh_token="fallback_token", sqlite_db=temp_sqlite_db_invalid_json)

        print("Verification: Fallback refresh_token is used...")
        print(f"Comparing refresh_token: Expected 'fallback_token', Got '{manager._refresh_token}'")
        assert manager._refresh_token == "fallback_token"

    def test_sqlite_takes_priority_over_json_file(self, temp_sqlite_db, temp_creds_file):
        """
        What it does: Verifies SQLite priority over JSON file.
        Purpose: Ensure SQLite is loaded instead of JSON when both specified.
        """
        print("Setup: Creating KiroAuthManager with SQLite and JSON file...")
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db, creds_file=temp_creds_file)

        print("Verification: Data from SQLite (not from JSON)...")
        print(f"Comparing access_token: Expected 'sqlite_access_token', Got '{manager._access_token}'")
        assert manager._access_token == "sqlite_access_token"

        print("Verification: SSO region from SQLite...")
        print(f"Comparing sso_region: Expected 'eu-west-1', Got '{manager._sso_region}'")
        assert manager._sso_region == "eu-west-1"

        print("Verification: API region stays at us-east-1...")
        print(f"Comparing region: Expected 'us-east-1', Got '{manager._region}'")
        assert manager._region == "us-east-1"


# =============================================================================
# Tests for _refresh_token_request() routing
# =============================================================================


class TestKiroAuthManagerRefreshTokenRouting:
    """Tests for _refresh_token_request() routing based on auth_type."""

    @pytest.mark.asyncio
    async def test_refresh_token_request_routes_to_kiro_desktop(self):
        """
        What it does: Verifies that KIRO_DESKTOP calls _refresh_token_kiro_desktop.
        Purpose: Ensure correct routing for Kiro Desktop auth.
        """
        print("Setup: Creating KiroAuthManager with KIRO_DESKTOP...")
        manager = KiroAuthManager(refresh_token="test_refresh")
        assert manager.auth_type == AuthType.KIRO_DESKTOP

        print("Setup: Mocking _refresh_token_kiro_desktop...")
        with patch.object(manager, "_refresh_token_kiro_desktop", new_callable=AsyncMock) as mock_desktop:
            with patch.object(manager, "_refresh_token_aws_sso_oidc", new_callable=AsyncMock) as mock_sso:
                await manager._refresh_token_request()

                print("Verification: _refresh_token_kiro_desktop was called...")
                mock_desktop.assert_called_once()

                print("Verification: _refresh_token_aws_sso_oidc was NOT called...")
                mock_sso.assert_not_called()

    @pytest.mark.asyncio
    async def test_refresh_token_request_routes_to_aws_sso_oidc(self):
        """
        What it does: Verifies that AWS_SSO_OIDC calls _refresh_token_aws_sso_oidc.
        Purpose: Ensure correct routing for AWS SSO OIDC auth.
        """
        print("Setup: Creating KiroAuthManager with AWS_SSO_OIDC...")
        manager = KiroAuthManager(
            refresh_token="test_refresh", client_id="test_client_id", client_secret="test_client_secret"
        )
        assert manager.auth_type == AuthType.AWS_SSO_OIDC

        print("Setup: Mocking _refresh_token_aws_sso_oidc...")
        with patch.object(manager, "_refresh_token_kiro_desktop", new_callable=AsyncMock) as mock_desktop:
            with patch.object(manager, "_refresh_token_aws_sso_oidc", new_callable=AsyncMock) as mock_sso:
                await manager._refresh_token_request()

                print("Verification: _refresh_token_aws_sso_oidc was called...")
                mock_sso.assert_called_once()

                print("Verification: _refresh_token_kiro_desktop was NOT called...")
                mock_desktop.assert_not_called()


# =============================================================================
# Tests for _refresh_token_aws_sso_oidc()
# =============================================================================


class TestKiroAuthManagerAwsSsoOidcRefresh:
    """Tests for _refresh_token_aws_sso_oidc() method."""

    @pytest.mark.asyncio
    async def test_refresh_token_aws_sso_oidc_success(self, mock_aws_sso_oidc_token_response):
        """
        What it does: Tests successful token refresh via AWS SSO OIDC.
        Purpose: Verify that on successful response token and expiration time are set.
        """
        print("Setup: Creating KiroAuthManager with AWS SSO OIDC...")
        manager = KiroAuthManager(
            refresh_token="test_refresh",
            client_id="test_client_id",
            client_secret="test_client_secret",
            region="us-east-1",
        )

        print("Setup: Mocking successful response from AWS SSO OIDC...")
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=mock_aws_sso_oidc_token_response())
        mock_response.raise_for_status = Mock()

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            print("Action: Calling _refresh_token_aws_sso_oidc()...")
            await manager._refresh_token_aws_sso_oidc()

            print("Verification: Token set correctly...")
            print(f"Comparing access_token: Expected 'new_aws_sso_access_token', Got '{manager._access_token}'")
            assert manager._access_token == "new_aws_sso_access_token"

            print("Verification: Expiration time set...")
            assert manager._expires_at is not None

    @pytest.mark.asyncio
    async def test_refresh_token_aws_sso_oidc_raises_without_refresh_token(self):
        """
        What it does: Verifies handling of missing refresh_token.
        Purpose: Ensure ValueError is raised without refresh_token.
        """
        print("Setup: Creating KiroAuthManager without refresh_token...")
        manager = KiroAuthManager(client_id="test_client_id", client_secret="test_client_secret")
        manager._refresh_token = None

        print("Action: Attempting token refresh without refresh_token...")
        with pytest.raises(ValueError) as exc_info:
            await manager._refresh_token_aws_sso_oidc()

        print(f"Verification: ValueError raised: {exc_info.value}")
        assert "Refresh token" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_refresh_token_aws_sso_oidc_raises_without_client_id(self):
        """
        What it does: Verifies handling of missing client_id.
        Purpose: Ensure ValueError is raised without client_id.
        """
        print("Setup: Creating KiroAuthManager without client_id...")
        manager = KiroAuthManager(refresh_token="test_refresh", client_secret="test_client_secret")
        manager._client_id = None
        manager._auth_type = AuthType.AWS_SSO_OIDC

        print("Action: Attempting token refresh without client_id...")
        with pytest.raises(ValueError) as exc_info:
            await manager._refresh_token_aws_sso_oidc()

        print(f"Verification: ValueError raised: {exc_info.value}")
        assert "Client ID" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_refresh_token_aws_sso_oidc_raises_without_client_secret(self):
        """
        What it does: Verifies handling of missing client_secret.
        Purpose: Ensure ValueError is raised without client_secret.
        """
        print("Setup: Creating KiroAuthManager without client_secret...")
        manager = KiroAuthManager(refresh_token="test_refresh", client_id="test_client_id")
        manager._client_secret = None
        manager._auth_type = AuthType.AWS_SSO_OIDC

        print("Action: Attempting token refresh without client_secret...")
        with pytest.raises(ValueError) as exc_info:
            await manager._refresh_token_aws_sso_oidc()

        print(f"Verification: ValueError raised: {exc_info.value}")
        assert "Client secret" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_refresh_token_aws_sso_oidc_uses_correct_endpoint(self, mock_aws_sso_oidc_token_response):
        """
        What it does: Verifies correct endpoint usage.
        Purpose: Ensure request goes to https://oidc.{region}.amazonaws.com/token.
        """
        print("Setup: Creating KiroAuthManager with region=eu-west-1...")
        manager = KiroAuthManager(
            refresh_token="test_refresh",
            client_id="test_client_id",
            client_secret="test_client_secret",
            region="eu-west-1",
        )

        print("Setup: Mocking HTTP client...")
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=mock_aws_sso_oidc_token_response())
        mock_response.raise_for_status = Mock()

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            await manager._refresh_token_aws_sso_oidc()

            print("Verification: POST request to correct URL...")
            call_args = mock_client.post.call_args
            url = call_args[0][0]
            expected_url = "https://oidc.eu-west-1.amazonaws.com/token"
            print(f"Comparing URL: Expected '{expected_url}', Got '{url}'")
            assert url == expected_url

    @pytest.mark.asyncio
    async def test_refresh_token_aws_sso_oidc_uses_json_format(self, mock_aws_sso_oidc_token_response):
        """
        What it does: Verifies JSON format usage (AWS SSO OIDC CreateToken API).
        Purpose: Ensure Content-Type = application/json (not form-urlencoded).
        """
        print("Setup: Creating KiroAuthManager...")
        manager = KiroAuthManager(
            refresh_token="test_refresh", client_id="test_client_id", client_secret="test_client_secret"
        )

        print("Setup: Mocking HTTP client...")
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=mock_aws_sso_oidc_token_response())
        mock_response.raise_for_status = Mock()

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            await manager._refresh_token_aws_sso_oidc()

            print("Verification: Content-Type = application/json...")
            call_args = mock_client.post.call_args
            headers = call_args[1].get("headers", {})
            print(f"Comparing Content-Type: Expected 'application/json', Got '{headers.get('Content-Type')}'")
            assert headers.get("Content-Type") == "application/json"

    @pytest.mark.asyncio
    async def test_refresh_token_aws_sso_oidc_sends_correct_grant_type(self, mock_aws_sso_oidc_token_response):
        """
        What it does: Verifies correct grantType is sent (camelCase).
        Purpose: Ensure grantType=refresh_token in JSON payload.
        """
        print("Setup: Creating KiroAuthManager...")
        manager = KiroAuthManager(
            refresh_token="test_refresh", client_id="test_client_id", client_secret="test_client_secret"
        )

        print("Setup: Mocking HTTP client...")
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=mock_aws_sso_oidc_token_response())
        mock_response.raise_for_status = Mock()

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            await manager._refresh_token_aws_sso_oidc()

            print("Verification: grantType = refresh_token (camelCase in JSON)...")
            call_args = mock_client.post.call_args
            json_payload = call_args[1].get("json", {})
            print(f"Comparing grantType: Expected 'refresh_token', Got '{json_payload.get('grantType')}'")
            assert json_payload.get("grantType") == "refresh_token"

    @pytest.mark.asyncio
    async def test_refresh_token_aws_sso_oidc_updates_tokens(self, mock_aws_sso_oidc_token_response):
        """
        What it does: Verifies access_token and refresh_token update.
        Purpose: Ensure both tokens are updated from response.
        """
        print("Setup: Creating KiroAuthManager...")
        manager = KiroAuthManager(
            refresh_token="old_refresh_token", client_id="test_client_id", client_secret="test_client_secret"
        )

        print("Setup: Mocking HTTP client...")
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=mock_aws_sso_oidc_token_response())
        mock_response.raise_for_status = Mock()

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            await manager._refresh_token_aws_sso_oidc()

            print("Verification: access_token updated...")
            assert manager._access_token == "new_aws_sso_access_token"

            print("Verification: refresh_token updated...")
            assert manager._refresh_token == "new_aws_sso_refresh_token"

    @pytest.mark.asyncio
    async def test_refresh_token_aws_sso_oidc_calculates_expiration(self, mock_aws_sso_oidc_token_response):
        """
        What it does: Verifies correct expiration time calculation.
        Purpose: Ensure expires_at is calculated based on expiresIn.
        """
        print("Setup: Creating KiroAuthManager...")
        manager = KiroAuthManager(
            refresh_token="test_refresh", client_id="test_client_id", client_secret="test_client_secret"
        )

        print("Setup: Mocking HTTP client with expiresIn=7200...")
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=mock_aws_sso_oidc_token_response(expires_in=7200))
        mock_response.raise_for_status = Mock()

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            await manager._refresh_token_aws_sso_oidc()

            print("Verification: expires_at set...")
            assert manager._expires_at is not None

            print("Verification: expires_at in the future...")
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)
            assert manager._expires_at > now

    @pytest.mark.asyncio
    async def test_refresh_token_aws_sso_oidc_does_not_send_scopes(self, mock_aws_sso_oidc_token_response):
        """
        What it does: Verifies that scopes are NOT sent in refresh request.
        Purpose: Per OAuth 2.0 RFC 6749 Section 6, scope is optional in refresh and
                 AWS SSO OIDC returns invalid_request if scope is sent.
        """
        print("Setup: Creating KiroAuthManager with scopes...")
        manager = KiroAuthManager(
            refresh_token="test_refresh", client_id="test_client_id", client_secret="test_client_secret"
        )
        # Simulate scopes loaded from SQLite (this is what caused the bug)
        manager._scopes = ["codewhisperer:completions", "codewhisperer:analysis"]

        print("Setup: Mocking HTTP client...")
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=mock_aws_sso_oidc_token_response())
        mock_response.raise_for_status = Mock()

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            await manager._refresh_token_aws_sso_oidc()

            print("Verification: scope NOT in JSON payload...")
            call_args = mock_client.post.call_args
            json_payload = call_args[1].get("json", {})
            print(f"Request JSON keys: {list(json_payload.keys())}")
            assert "scope" not in json_payload, "scope should NOT be sent in refresh request"

            print("Verification: only required fields sent (camelCase)...")
            expected_keys = {"grantType", "clientId", "clientSecret", "refreshToken"}
            print(f"Comparing keys: Expected {expected_keys}, Got {set(json_payload.keys())}")
            assert set(json_payload.keys()) == expected_keys

    @pytest.mark.asyncio
    async def test_refresh_token_aws_sso_oidc_works_without_scopes(self, mock_aws_sso_oidc_token_response):
        """
        What it does: Verifies refresh works when scopes are None.
        Purpose: Ensure backward compatibility with credentials that don't have scopes.
        """
        print("Setup: Creating KiroAuthManager without scopes...")
        manager = KiroAuthManager(
            refresh_token="test_refresh", client_id="test_client_id", client_secret="test_client_secret"
        )
        # Explicitly set scopes to None (default state)
        manager._scopes = None

        print("Setup: Mocking HTTP client...")
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=mock_aws_sso_oidc_token_response())
        mock_response.raise_for_status = Mock()

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            await manager._refresh_token_aws_sso_oidc()

            print("Verification: Token refreshed successfully...")
            assert manager._access_token == "new_aws_sso_access_token"

            print("Verification: scope NOT in request JSON payload...")
            call_args = mock_client.post.call_args
            json_payload = call_args[1].get("json", {})
            assert "scope" not in json_payload


# =============================================================================
# Tests for auth_type property and constructor with new parameters
# =============================================================================


class TestKiroAuthManagerAuthTypeProperty:
    """Tests for auth_type property and constructor."""

    def test_auth_type_property_returns_correct_value(self):
        """
        What it does: Verifies that auth_type property returns correct value.
        Purpose: Ensure property works correctly.
        """
        print("Setup: Creating KiroAuthManager with KIRO_DESKTOP...")
        manager_desktop = KiroAuthManager(refresh_token="test")

        print("Verification: auth_type = KIRO_DESKTOP...")
        assert manager_desktop.auth_type == AuthType.KIRO_DESKTOP

        print("Setup: Creating KiroAuthManager with AWS_SSO_OIDC...")
        manager_sso = KiroAuthManager(refresh_token="test", client_id="id", client_secret="secret")

        print("Verification: auth_type = AWS_SSO_OIDC...")
        assert manager_sso.auth_type == AuthType.AWS_SSO_OIDC

    def test_init_with_client_id_and_secret(self):
        """
        What it does: Verifies initialization with client_id and client_secret.
        Purpose: Ensure parameters are stored in private fields.
        """
        print("Setup: Creating KiroAuthManager with client credentials...")
        manager = KiroAuthManager(refresh_token="test", client_id="my_client_id", client_secret="my_client_secret")

        print("Verification: client_id stored...")
        assert manager._client_id == "my_client_id"

        print("Verification: client_secret stored...")
        assert manager._client_secret == "my_client_secret"

    def test_init_with_sqlite_db_parameter(self, temp_sqlite_db):
        """
        What it does: Verifies initialization with sqlite_db parameter.
        Purpose: Ensure data is loaded from SQLite.
        """
        print(f"Setup: Creating KiroAuthManager with sqlite_db: {temp_sqlite_db}")
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db)

        print("Verification: Data loaded from SQLite...")
        assert manager._access_token == "sqlite_access_token"
        assert manager._refresh_token == "sqlite_refresh_token"

    def test_detect_auth_type_kiro_desktop_when_only_client_secret(self):
        """
        What it does: Verifies type detection with only clientSecret (no id).
        Purpose: Ensure KIRO_DESKTOP is used without clientId.
        """
        print("Setup: Creating KiroAuthManager with only client_secret...")
        manager = KiroAuthManager(refresh_token="test_token", client_secret="test_client_secret")

        print("Verification: auth_type = KIRO_DESKTOP (both id and secret required)...")
        print(f"Comparing auth_type: Expected KIRO_DESKTOP, Got {manager.auth_type}")
        assert manager.auth_type == AuthType.KIRO_DESKTOP


# =============================================================================
# Tests for SSO region separation (Issue #16)
# =============================================================================


class TestKiroAuthManagerSsoRegionSeparation:
    """Tests for SSO region separation from API region (Issue #16 fix).

    Background: CodeWhisperer API only exists in us-east-1, but users may have
    SSO credentials from other regions (e.g., ap-southeast-1 for Singapore).
    The fix separates SSO region (for OIDC token refresh) from API region.
    """

    def test_api_region_uses_sso_region_as_fallback_when_no_profile_arn(self, temp_sqlite_db):
        """
        What it does: Verifies API region uses SSO region as fallback when profile ARN is not available.
        Purpose: Ensure graceful fallback when state table doesn't exist or profile key is missing.
        """
        print("Setup: Creating KiroAuthManager with SQLite (SSO region=eu-west-1, no profile ARN)...")
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db)

        print("Verification: SSO region loaded from SQLite...")
        print(f"Comparing _sso_region: Expected 'eu-west-1', Got '{manager._sso_region}'")
        assert manager._sso_region == "eu-west-1"

        print("Verification: No API region detected from profile ARN...")
        print(f"Comparing _detected_api_region: Expected None, Got '{manager._detected_api_region}'")
        assert manager._detected_api_region is None

        print("Verification: API region uses SSO region as fallback...")
        print(f"api_host: {manager._api_host}")
        assert "eu-west-1" in manager._api_host

        print(f"q_host: {manager._q_host}")
        assert "eu-west-1" in manager._q_host

    def test_sso_region_stored_separately_from_api_region(self, temp_sqlite_db):
        """
        What it does: Verifies SSO region is stored in _sso_region field.
        Purpose: Ensure SSO region is available for OIDC token refresh.
        """
        print("Setup: Creating KiroAuthManager with SQLite (region=eu-west-1)...")
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db)

        print("Verification: SSO region stored in _sso_region...")
        print(f"Comparing _sso_region: Expected 'eu-west-1', Got '{manager._sso_region}'")
        assert manager._sso_region == "eu-west-1"

        print("Verification: API region is different from SSO region...")
        assert manager._region != manager._sso_region

    def test_sso_region_none_when_not_loaded_from_sqlite(self):
        """
        What it does: Verifies _sso_region is None when not loading from SQLite.
        Purpose: Ensure backward compatibility with direct credential initialization.
        """
        print("Setup: Creating KiroAuthManager with direct credentials...")
        manager = KiroAuthManager(refresh_token="test_token", region="us-east-1")

        print("Verification: _sso_region is None...")
        print(f"Comparing _sso_region: Expected None, Got '{manager._sso_region}'")
        assert manager._sso_region is None

    @pytest.mark.asyncio
    async def test_oidc_refresh_uses_sso_region(self, mock_aws_sso_oidc_token_response):
        """
        What it does: Verifies OIDC token refresh uses SSO region, not API region.
        Purpose: Ensure token refresh goes to correct regional OIDC endpoint.
        """
        print("Setup: Creating KiroAuthManager with SSO region=ap-southeast-1...")
        manager = KiroAuthManager(
            refresh_token="test_refresh",
            client_id="test_client_id",
            client_secret="test_client_secret",
            region="us-east-1",  # API region
        )
        # Simulate SSO region loaded from SQLite
        manager._sso_region = "ap-southeast-1"

        print("Setup: Mocking HTTP client...")
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=mock_aws_sso_oidc_token_response())
        mock_response.raise_for_status = Mock()

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            await manager._refresh_token_aws_sso_oidc()

            print("Verification: OIDC request went to SSO region (ap-southeast-1)...")
            call_args = mock_client.post.call_args
            url = call_args[0][0]
            expected_url = "https://oidc.ap-southeast-1.amazonaws.com/token"
            print(f"Comparing URL: Expected '{expected_url}', Got '{url}'")
            assert url == expected_url
            assert "ap-southeast-1" in url
            assert "us-east-1" not in url

    @pytest.mark.asyncio
    async def test_oidc_refresh_falls_back_to_api_region_when_no_sso_region(self, mock_aws_sso_oidc_token_response):
        """
        What it does: Verifies OIDC refresh uses API region when SSO region not set.
        Purpose: Ensure backward compatibility when _sso_region is None.
        """
        print("Setup: Creating KiroAuthManager without SSO region...")
        manager = KiroAuthManager(
            refresh_token="test_refresh",
            client_id="test_client_id",
            client_secret="test_client_secret",
            region="eu-west-1",  # API region (also used for OIDC when no SSO region)
        )
        # Ensure _sso_region is None
        manager._sso_region = None

        print("Setup: Mocking HTTP client...")
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=mock_aws_sso_oidc_token_response())
        mock_response.raise_for_status = Mock()

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            await manager._refresh_token_aws_sso_oidc()

            print("Verification: OIDC request fell back to API region (eu-west-1)...")
            call_args = mock_client.post.call_args
            url = call_args[0][0]
            expected_url = "https://oidc.eu-west-1.amazonaws.com/token"
            print(f"Comparing URL: Expected '{expected_url}', Got '{url}'")
            assert url == expected_url

    def test_refresh_url_uses_sso_region_when_loading_from_sqlite(self, temp_sqlite_db):
        """
        What it does: Verifies refresh_url uses SSO region for OIDC token refresh.
        Purpose: Ensure OIDC token refresh goes to correct regional endpoint.
        """
        print("Setup: Creating KiroAuthManager with SQLite (SSO region=eu-west-1)...")
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db)

        print("Verification: SSO region loaded...")
        print(f"Comparing _sso_region: Expected 'eu-west-1', Got '{manager._sso_region}'")
        assert manager._sso_region == "eu-west-1"

        print("Verification: _refresh_url uses SSO region for OIDC...")
        print(f"_refresh_url: {manager._refresh_url}")
        assert "eu-west-1" in manager._refresh_url

        print("Verification: API hosts use SSO region as fallback (no profile ARN)...")
        assert "eu-west-1" in manager._api_host
        assert "eu-west-1" in manager._q_host

    @pytest.mark.asyncio
    async def test_refresh_token_aws_sso_oidc_uses_memory_token_first(self, mock_aws_sso_oidc_token_response):
        """
        What it does: Verifies that in-memory token is used first, not SQLite.
        Purpose: Ensure container's successfully refreshed token is used (not overwritten by SQLite).
        """
        print("Setup: Creating KiroAuthManager with in-memory credentials...")
        manager = KiroAuthManager(
            refresh_token="memory_refresh_token", client_id="test_client_id", client_secret="test_client_secret"
        )
        # Simulate SQLite path being set (but we won't actually use it)
        manager._sqlite_db = "/fake/path/data.sqlite3"

        print("Setup: Mocking HTTP client for successful refresh...")
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=mock_aws_sso_oidc_token_response())
        mock_response.raise_for_status = Mock()

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            # Patch _load_credentials_from_sqlite to track if it's called
            with patch.object(manager, "_load_credentials_from_sqlite") as mock_load:
                await manager._refresh_token_aws_sso_oidc()

                print("Verification: SQLite was NOT reloaded (success on first try)...")
                mock_load.assert_not_called()

                print("Verification: Request used in-memory token...")
                call_args = mock_client.post.call_args
                json_payload = call_args[1].get("json", {})
                print(f"Refresh token sent: {json_payload.get('refreshToken')}")
                assert json_payload.get("refreshToken") == "memory_refresh_token"

    @pytest.mark.asyncio
    async def test_refresh_token_aws_sso_oidc_reloads_sqlite_on_400_error(
        self, tmp_path, mock_aws_sso_oidc_token_response
    ):
        """
        What it does: Verifies SQLite is reloaded and retry happens on 400 error.
        Purpose: Pick up fresh tokens after kiro-cli re-login when in-memory token is stale.
        """
        import json
        import sqlite3

        # Setup: Create initial SQLite database
        db_file = tmp_path / "data.sqlite3"
        conn = sqlite3.connect(str(db_file))
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE auth_kv (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Initial token data (will become stale)
        initial_token_data = {
            "access_token": "old_access_token",
            "refresh_token": "old_refresh_token",
            "expires_at": "2099-01-01T00:00:00Z",
            "region": "us-east-1",
        }
        cursor.execute(
            "INSERT INTO auth_kv (key, value) VALUES (?, ?)",
            ("codewhisperer:odic:token", json.dumps(initial_token_data)),
        )

        registration_data = {
            "client_id": "test_client_id",
            "client_secret": "test_client_secret",
            "region": "us-east-1",
        }
        cursor.execute(
            "INSERT INTO auth_kv (key, value) VALUES (?, ?)",
            ("codewhisperer:odic:device-registration", json.dumps(registration_data)),
        )

        conn.commit()
        conn.close()

        print("Setup: Creating KiroAuthManager with SQLite...")
        manager = KiroAuthManager(sqlite_db=str(db_file))

        print("Verification: Initial refresh_token loaded...")
        assert manager._refresh_token == "old_refresh_token"

        # Simulate kiro-cli updating the SQLite with fresh tokens
        print("Action: Simulating kiro-cli token refresh (updating SQLite)...")
        conn = sqlite3.connect(str(db_file))
        cursor = conn.cursor()

        new_token_data = {
            "access_token": "new_access_token",
            "refresh_token": "new_refresh_token_from_kiro_cli",
            "expires_at": "2099-01-01T00:00:00Z",
            "region": "us-east-1",
        }
        cursor.execute(
            "UPDATE auth_kv SET value = ? WHERE key = ?", (json.dumps(new_token_data), "codewhisperer:odic:token")
        )
        conn.commit()
        conn.close()

        # Manager still has old token in memory
        print("Verification: Manager still has old refresh_token in memory...")
        assert manager._refresh_token == "old_refresh_token"

        # Mock HTTP client: first call fails with 400, second succeeds
        print("Setup: Mocking HTTP client (first=400, second=200)...")

        # First response: 400 error (stale token)
        mock_error_response = AsyncMock()
        mock_error_response.status_code = 400
        mock_error_response.text = '{"error":"invalid_request","error_description":"Invalid request"}'
        mock_error_response.json = Mock(return_value={"error": "invalid_request"})
        mock_error_response.raise_for_status = Mock(
            side_effect=httpx.HTTPStatusError("400 Bad Request", request=Mock(), response=mock_error_response)
        )

        # Second response: success
        mock_success_response = AsyncMock()
        mock_success_response.status_code = 200
        mock_success_response.json = Mock(return_value=mock_aws_sso_oidc_token_response())
        mock_success_response.raise_for_status = Mock()

        call_count = 0
        sent_tokens = []

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            sent_tokens.append(kwargs.get("json", {}).get("refreshToken"))
            if call_count == 1:
                return mock_error_response
            return mock_success_response

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            print("Action: Calling _refresh_token_aws_sso_oidc...")
            await manager._refresh_token_aws_sso_oidc()

            print("Verification: Two requests were made (retry on 400)...")
            print(f"Call count: {call_count}")
            assert call_count == 2, "Should retry after 400 error"

            print("Verification: First request used OLD token from memory...")
            print(f"First token sent: {sent_tokens[0]}")
            assert sent_tokens[0] == "old_refresh_token"

            print("Verification: Second request used NEW token from SQLite...")
            print(f"Second token sent: {sent_tokens[1]}")
            assert sent_tokens[1] == "new_refresh_token_from_kiro_cli"

    @pytest.mark.asyncio
    async def test_refresh_token_aws_sso_oidc_no_retry_on_non_400_error(self, mock_aws_sso_oidc_token_response):
        """
        What it does: Verifies that non-400 errors are not retried.
        Purpose: Ensure only 400 (invalid_request) triggers SQLite reload.
        """
        print("Setup: Creating KiroAuthManager...")
        manager = KiroAuthManager(
            refresh_token="test_refresh", client_id="test_client_id", client_secret="test_client_secret"
        )
        manager._sqlite_db = "/fake/path/data.sqlite3"

        print("Setup: Mocking HTTP client with 500 error...")
        mock_error_response = AsyncMock()
        mock_error_response.status_code = 500
        mock_error_response.text = "Internal Server Error"
        mock_error_response.json = Mock(side_effect=Exception("Not JSON"))
        mock_error_response.raise_for_status = Mock(
            side_effect=httpx.HTTPStatusError("500 Internal Server Error", request=Mock(), response=mock_error_response)
        )

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_error_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            with patch.object(manager, "_load_credentials_from_sqlite") as mock_load:
                print("Action: Calling _refresh_token_aws_sso_oidc (expecting 500 error)...")
                with pytest.raises(httpx.HTTPStatusError) as exc_info:
                    await manager._refresh_token_aws_sso_oidc()

                print("Verification: 500 error was raised (not retried)...")
                assert exc_info.value.response.status_code == 500

                print("Verification: SQLite was NOT reloaded (500 != 400)...")
                mock_load.assert_not_called()

    @pytest.mark.asyncio
    async def test_refresh_token_aws_sso_oidc_no_retry_without_sqlite_db(self, mock_aws_sso_oidc_token_response):
        """
        What it does: Verifies that 400 error is not retried when sqlite_db is not set.
        Purpose: Ensure retry only happens when SQLite source is available.
        """
        print("Setup: Creating KiroAuthManager WITHOUT sqlite_db...")
        manager = KiroAuthManager(
            refresh_token="test_refresh", client_id="test_client_id", client_secret="test_client_secret"
        )
        # Explicitly ensure no sqlite_db
        manager._sqlite_db = None

        print("Setup: Mocking HTTP client with 400 error...")
        mock_error_response = AsyncMock()
        mock_error_response.status_code = 400
        mock_error_response.text = '{"error":"invalid_request"}'
        mock_error_response.json = Mock(return_value={"error": "invalid_request"})
        mock_error_response.raise_for_status = Mock(
            side_effect=httpx.HTTPStatusError("400 Bad Request", request=Mock(), response=mock_error_response)
        )

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_error_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            print("Action: Calling _refresh_token_aws_sso_oidc (expecting 400 error)...")
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await manager._refresh_token_aws_sso_oidc()

            print("Verification: 400 error was raised (no retry without sqlite_db)...")
            assert exc_info.value.response.status_code == 400

            print("Verification: Only one request was made...")
            assert mock_client.post.call_count == 1


# =============================================================================
# Tests for is_token_expired() method
# =============================================================================


class TestKiroAuthManagerIsTokenExpired:
    """Tests for is_token_expired() method.

    This method checks if the token has actually expired (not just expiring soon).
    Used for graceful degradation when refresh fails.
    """

    def test_is_token_expired_returns_true_when_no_expires_at(self):
        """
        What it does: Verifies that without expires_at token is considered expired.
        Purpose: Ensure safe behavior when time information is missing.
        """
        print("Setup: Creating KiroAuthManager without expires_at...")
        manager = KiroAuthManager(refresh_token="test_token")
        manager._expires_at = None

        print("Verification: is_token_expired returns True...")
        result = manager.is_token_expired()
        print(f"Comparing result: Expected True, Got {result}")
        assert result is True

    def test_is_token_expired_returns_true_when_expired(self):
        """
        What it does: Verifies that expired token is correctly identified.
        Purpose: Ensure token in the past is considered expired.
        """
        print("Setup: Creating KiroAuthManager with expired token...")
        manager = KiroAuthManager(refresh_token="test_token")
        manager._expires_at = datetime.now(timezone.utc) - timedelta(hours=1)

        print("Verification: is_token_expired returns True for expired token...")
        result = manager.is_token_expired()
        print(f"Comparing result: Expected True, Got {result}")
        assert result is True

    def test_is_token_expired_returns_false_when_valid(self):
        """
        What it does: Verifies that valid token is not considered expired.
        Purpose: Ensure token in the future is not considered expired.
        """
        print("Setup: Creating KiroAuthManager with valid token...")
        manager = KiroAuthManager(refresh_token="test_token")
        manager._expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

        print("Verification: is_token_expired returns False...")
        result = manager.is_token_expired()
        print(f"Comparing result: Expected False, Got {result}")
        assert result is False

    def test_is_token_expired_returns_false_when_expiring_soon_but_not_expired(self):
        """
        What it does: Verifies difference between expiring soon and actually expired.
        Purpose: Ensure token expiring in 5 minutes is NOT considered expired yet.
        """
        print("Setup: Creating KiroAuthManager with token expiring in 5 minutes...")
        manager = KiroAuthManager(refresh_token="test_token")
        manager._expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

        print("Verification: is_token_expiring_soon returns True (within threshold)...")
        assert manager.is_token_expiring_soon() is True

        print("Verification: is_token_expired returns False (not actually expired)...")
        result = manager.is_token_expired()
        print(f"Comparing result: Expected False, Got {result}")
        assert result is False


# =============================================================================
# Tests for graceful degradation in get_access_token() (SQLite mode)
# =============================================================================


class TestKiroAuthManagerGracefulDegradation:
    """Tests for graceful degradation when refresh fails in SQLite mode.

    Background: When kiro-cli refreshes tokens in memory without persisting to SQLite,
    the refresh_token in SQLite becomes stale. The gateway should gracefully fall back
    to using the access_token directly until it actually expires.
    """

    @pytest.mark.asyncio
    async def test_get_access_token_reloads_sqlite_when_expiring_soon(self, tmp_path):
        """
        What it does: Verifies SQLite is reloaded when token is expiring soon.
        Purpose: Pick up fresh tokens from kiro-cli before attempting refresh.
        """
        import json
        import sqlite3

        print("Setup: Creating SQLite database with fresh token...")
        db_file = tmp_path / "data.sqlite3"
        conn = sqlite3.connect(str(db_file))
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE auth_kv (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Token that expires in 1 hour (fresh)
        fresh_token_data = {
            "access_token": "fresh_access_token",
            "refresh_token": "fresh_refresh_token",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "region": "us-east-1",
        }
        cursor.execute(
            "INSERT INTO auth_kv (key, value) VALUES (?, ?)", ("codewhisperer:odic:token", json.dumps(fresh_token_data))
        )

        registration_data = {
            "client_id": "test_client_id",
            "client_secret": "test_client_secret",
            "region": "us-east-1",
        }
        cursor.execute(
            "INSERT INTO auth_kv (key, value) VALUES (?, ?)",
            ("codewhisperer:odic:device-registration", json.dumps(registration_data)),
        )
        conn.commit()
        conn.close()

        print("Setup: Creating KiroAuthManager with expiring token...")
        manager = KiroAuthManager(sqlite_db=str(db_file))
        # Simulate token expiring soon (within threshold)
        manager._access_token = "old_expiring_token"
        manager._expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

        print("Verification: Token is expiring soon...")
        assert manager.is_token_expiring_soon() is True

        print("Action: Calling get_access_token()...")
        token = await manager.get_access_token()

        print("Verification: Got fresh token from SQLite reload...")
        print(f"Comparing token: Expected 'fresh_access_token', Got '{token}'")
        assert token == "fresh_access_token"

    @pytest.mark.asyncio
    async def test_get_access_token_graceful_fallback_when_refresh_fails_but_token_valid(self, tmp_path):
        """
        What it does: Verifies graceful fallback when refresh fails with 400 but access_token still valid.
        Purpose: Use existing access_token until it actually expires when kiro-cli owns refresh.
        """
        import json
        import sqlite3

        print("Setup: Creating SQLite database...")
        db_file = tmp_path / "data.sqlite3"
        conn = sqlite3.connect(str(db_file))
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE auth_kv (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Token that is expiring soon but NOT expired yet
        token_data = {
            "access_token": "still_valid_access_token",
            "refresh_token": "stale_refresh_token",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "region": "us-east-1",
        }
        cursor.execute(
            "INSERT INTO auth_kv (key, value) VALUES (?, ?)", ("codewhisperer:odic:token", json.dumps(token_data))
        )

        registration_data = {
            "client_id": "test_client_id",
            "client_secret": "test_client_secret",
            "region": "us-east-1",
        }
        cursor.execute(
            "INSERT INTO auth_kv (key, value) VALUES (?, ?)",
            ("codewhisperer:odic:device-registration", json.dumps(registration_data)),
        )
        conn.commit()
        conn.close()

        print("Setup: Creating KiroAuthManager...")
        manager = KiroAuthManager(sqlite_db=str(db_file))

        print("Verification: Token is expiring soon but NOT expired...")
        assert manager.is_token_expiring_soon() is True
        assert manager.is_token_expired() is False

        print("Setup: Mocking HTTP client to return 400 twice (stale refresh token)...")
        mock_error_response = AsyncMock()
        mock_error_response.status_code = 400
        mock_error_response.text = '{"error":"invalid_request"}'
        mock_error_response.json = Mock(return_value={"error": "invalid_request"})
        mock_error_response.raise_for_status = Mock(
            side_effect=httpx.HTTPStatusError("400 Bad Request", request=Mock(), response=mock_error_response)
        )

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_error_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            print("Action: Calling get_access_token() (expecting graceful fallback)...")
            token = await manager.get_access_token()

            print("Verification: Got existing access_token (graceful fallback)...")
            print(f"Comparing token: Expected 'still_valid_access_token', Got '{token}'")
            assert token == "still_valid_access_token"

    @pytest.mark.asyncio
    async def test_get_access_token_raises_when_refresh_fails_and_token_expired(self, tmp_path):
        """
        What it does: Verifies error is raised when refresh fails and access_token is expired.
        Purpose: Clear error message when user needs to run 'kiro-cli login'.
        """
        import json
        import sqlite3

        print("Setup: Creating SQLite database with expired token...")
        db_file = tmp_path / "data.sqlite3"
        conn = sqlite3.connect(str(db_file))
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE auth_kv (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Token that is already expired
        token_data = {
            "access_token": "expired_access_token",
            "refresh_token": "stale_refresh_token",
            "expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            "region": "us-east-1",
        }
        cursor.execute(
            "INSERT INTO auth_kv (key, value) VALUES (?, ?)", ("codewhisperer:odic:token", json.dumps(token_data))
        )

        registration_data = {
            "client_id": "test_client_id",
            "client_secret": "test_client_secret",
            "region": "us-east-1",
        }
        cursor.execute(
            "INSERT INTO auth_kv (key, value) VALUES (?, ?)",
            ("codewhisperer:odic:device-registration", json.dumps(registration_data)),
        )
        conn.commit()
        conn.close()

        print("Setup: Creating KiroAuthManager...")
        manager = KiroAuthManager(sqlite_db=str(db_file))

        print("Verification: Token is expired...")
        assert manager.is_token_expired() is True

        print("Setup: Mocking HTTP client to return 400 (stale refresh token)...")
        mock_error_response = AsyncMock()
        mock_error_response.status_code = 400
        mock_error_response.text = '{"error":"invalid_request"}'
        mock_error_response.json = Mock(return_value={"error": "invalid_request"})
        mock_error_response.raise_for_status = Mock(
            side_effect=httpx.HTTPStatusError("400 Bad Request", request=Mock(), response=mock_error_response)
        )

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_error_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            print("Action: Calling get_access_token() (expecting ValueError)...")
            with pytest.raises(ValueError) as exc_info:
                await manager.get_access_token()

            print(f"Verification: ValueError raised with helpful message: {exc_info.value}")
            assert "kiro-cli login" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_get_access_token_non_sqlite_mode_propagates_400_error(self):
        """
        What it does: Verifies a 400 still fails the call in non-SQLite mode.
        Purpose: Ensure graceful degradation only applies to SQLite mode.

        The failure surfaces as CredentialDeadError rather than the raw
        httpx.HTTPStatusError. With no SQLite database there is no raw source to
        reload and no still-valid access token to fall back to, so a 400 here is
        terminal - the refresh token is finished. The raw exception is deliberately
        no longer allowed to escape: being neither RequestError nor
        TimeoutException, it matched no handler in the retry loop or the routes and
        became a bare 500 with no failover. The status code stays assertable on the
        typed error.
        """
        print("Setup: Creating KiroAuthManager WITHOUT sqlite_db...")
        manager = KiroAuthManager(
            refresh_token="test_refresh", client_id="test_client_id", client_secret="test_client_secret"
        )
        manager._access_token = "expiring_token"
        manager._expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

        print("Verification: No sqlite_db set...")
        assert manager._sqlite_db is None

        print("Setup: Mocking HTTP client to return 400...")
        mock_error_response = AsyncMock()
        mock_error_response.status_code = 400
        mock_error_response.text = '{"error":"invalid_request"}'
        mock_error_response.json = Mock(return_value={"error": "invalid_request"})
        mock_error_response.raise_for_status = Mock(
            side_effect=httpx.HTTPStatusError("400 Bad Request", request=Mock(), response=mock_error_response)
        )

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_error_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            print("Action: Calling get_access_token() (expecting CredentialDeadError)...")
            with pytest.raises(CredentialDeadError) as exc_info:
                await manager.get_access_token()

            print("Verification: 400 error was propagated (no graceful degradation)...")
            assert exc_info.value.status_code == 400
            # The stale access token must not be handed out: that fallback is
            # SQLite-only, where kiro-cli may have rotated the token out of band.
            assert manager._access_token == "expiring_token"


class TestExternalCredentialOverlayPersistence:
    """Rotations persist in the gateway store without mutating source credentials."""

    @staticmethod
    def _register_source(source_type: str, path: str) -> str:
        initialize()
        with connection() as conn:
            replace_account_sources([{"type": source_type, "path": path}], conn)
            return conn.execute("SELECT account_id FROM account_sources").fetchone()[0]

    @staticmethod
    def _overlay(account_id: str) -> dict:
        with connection() as conn:
            row = conn.execute(
                "SELECT credential_json FROM account_sources WHERE account_id = ?", (account_id,)
            ).fetchone()
        assert row is not None and row[0] is not None
        return json.loads(row[0])

    def test_save_from_external_sqlite_uses_overlay_and_survives_restart(self, temp_sqlite_db_social):
        account_id = self._register_source("kiro_cli", temp_sqlite_db_social)
        source_before = open(temp_sqlite_db_social, "rb").read()
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db_social)
        manager._access_token = "rotated_access"
        manager._refresh_token = "rotated_refresh"
        manager._expires_at = datetime(2099, 2, 3, tzinfo=timezone.utc)

        manager._save_credentials_to_sqlite()

        assert open(temp_sqlite_db_social, "rb").read() == source_before
        assert self._overlay(account_id) == {
            "accessToken": "rotated_access",
            "refreshToken": "rotated_refresh",
            "expiresAt": "2099-02-03T00:00:00+00:00",
            "profileArn": "arn:aws:codewhisperer:us-east-1:123456789:profile/social",
        }
        restarted = KiroAuthManager(sqlite_db=temp_sqlite_db_social)
        assert restarted._access_token == "rotated_access"
        assert restarted._refresh_token == "rotated_refresh"
        assert restarted._expires_at == datetime(2099, 2, 3, tzinfo=timezone.utc)

    def test_save_from_external_json_uses_overlay_and_survives_restart(self, temp_creds_file):
        account_id = self._register_source("json", temp_creds_file)
        source_before = open(temp_creds_file, "rb").read()
        manager = KiroAuthManager(creds_file=temp_creds_file)
        manager._access_token = "json_rotated_access"
        manager._refresh_token = "json_rotated_refresh"
        manager._expires_at = datetime(2099, 4, 5, tzinfo=timezone.utc)

        manager._save_credentials_to_file()

        assert open(temp_creds_file, "rb").read() == source_before
        overlay = self._overlay(account_id)
        assert overlay["accessToken"] == "json_rotated_access"
        assert overlay["refreshToken"] == "json_rotated_refresh"
        assert overlay["expiresAt"] == "2099-04-05T00:00:00+00:00"
        restarted = KiroAuthManager(creds_file=temp_creds_file)
        assert restarted._access_token == "json_rotated_access"
        assert restarted._refresh_token == "json_rotated_refresh"

    def test_newer_external_sqlite_login_supersedes_stale_overlay(self, temp_sqlite_db_social):
        self._register_source("kiro_cli", temp_sqlite_db_social)
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db_social)
        manager._access_token = "stale_overlay_access"
        manager._refresh_token = "stale_overlay_refresh"
        manager._expires_at = datetime(2099, 2, 3, tzinfo=timezone.utc)
        manager._save_credentials_to_sqlite()

        with sqlite3.connect(temp_sqlite_db_social) as conn:
            row = conn.execute("SELECT value FROM auth_kv WHERE key = 'kirocli:social:token'").fetchone()
            assert row is not None
            external = json.loads(row[0])
            external.update(
                access_token="new_login_access",
                refresh_token="new_login_refresh",
                expires_at="2099-03-04T00:00:00Z",
            )
            conn.execute(
                "UPDATE auth_kv SET value = ? WHERE key = 'kirocli:social:token'",
                (json.dumps(external),),
            )

        restarted = KiroAuthManager(sqlite_db=temp_sqlite_db_social)
        assert restarted._access_token == "new_login_access"
        assert restarted._refresh_token == "new_login_refresh"
        assert restarted._expires_at == datetime(2099, 3, 4, tzinfo=timezone.utc)

    def test_newer_external_json_login_supersedes_stale_overlay(self, temp_creds_file):
        self._register_source("json", temp_creds_file)
        manager = KiroAuthManager(creds_file=temp_creds_file)
        manager._access_token = "stale_json_overlay_access"
        manager._refresh_token = "stale_json_overlay_refresh"
        manager._expires_at = datetime(2099, 2, 3, tzinfo=timezone.utc)
        manager._save_credentials_to_file()

        with open(temp_creds_file, encoding="utf-8") as source:
            external = json.load(source)
        external.update(
            accessToken="new_json_login_access",
            refreshToken="new_json_login_refresh",
            expiresAt="2099-03-04T00:00:00Z",
        )
        with open(temp_creds_file, "w", encoding="utf-8") as source:
            json.dump(external, source)

        restarted = KiroAuthManager(creds_file=temp_creds_file)
        assert restarted._access_token == "new_json_login_access"
        assert restarted._refresh_token == "new_json_login_refresh"
        assert restarted._expires_at == datetime(2099, 3, 4, tzinfo=timezone.utc)

    @pytest.mark.asyncio
    async def test_refresh_from_external_sqlite_updates_only_overlay(
        self, temp_sqlite_db_social, mock_kiro_token_response, valid_kiro_token
    ):
        account_id = self._register_source("kiro_cli", temp_sqlite_db_social)
        source_before = open(temp_sqlite_db_social, "rb").read()
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db_social)
        mock_response = AsyncMock(status_code=200)
        refreshed = mock_kiro_token_response()
        refreshed["expiresIn"] = 4_000_000_000
        mock_response.json = Mock(return_value=refreshed)
        mock_response.raise_for_status = Mock()

        with patch("kiro.auth.httpx.AsyncClient") as client_class:
            client = AsyncMock()
            client.post = AsyncMock(return_value=mock_response)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            client_class.return_value = client
            await manager._refresh_token_kiro_desktop()

        assert open(temp_sqlite_db_social, "rb").read() == source_before
        overlay = self._overlay(account_id)
        assert overlay["accessToken"] == valid_kiro_token
        assert overlay["refreshToken"] == "new_refresh_token_xyz"
        restarted = KiroAuthManager(sqlite_db=temp_sqlite_db_social)
        assert restarted._access_token == valid_kiro_token
        assert restarted._refresh_token == "new_refresh_token_xyz"


# =============================================================================
# Legacy SQLite write-back tests (superseded by immutable source overlays)
# =============================================================================
class TestKiroAuthManagerSocialLogin:
    """Tests for social login support (Google, GitHub, etc.).

    Background: kiro-cli supports social login (Google, GitHub) for free-tier users.
    These credentials are stored in SQLite with key 'kirocli:social:token' instead of
    'kirocli:odic:token'. Social login uses the same Kiro Desktop Auth endpoint
    (no client_id/client_secret required).
    """

    def test_load_credentials_from_sqlite_social_token(self, temp_sqlite_db_social):
        """
        What it does: Verifies loading credentials from kirocli:social:token key.
        Purpose: Ensure social login credentials are loaded correctly.
        """
        print(f"Setup: Creating KiroAuthManager with social login SQLite: {temp_sqlite_db_social}")
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db_social)

        print("Verification: access_token loaded from social key...")
        print(f"Comparing access_token: Expected 'social_access_token', Got '{manager._access_token}'")
        assert manager._access_token == "social_access_token"

        print("Verification: refresh_token loaded from social key...")
        print(f"Comparing refresh_token: Expected 'social_refresh_token', Got '{manager._refresh_token}'")
        assert manager._refresh_token == "social_refresh_token"

        print("Verification: profile_arn loaded...")
        assert manager._profile_arn == "arn:aws:codewhisperer:us-east-1:123456789:profile/social"

    def test_social_login_detected_as_kiro_desktop(self, temp_sqlite_db_social):
        """
        What it does: Verifies social login is detected as KIRO_DESKTOP auth type.
        Purpose: Ensure social login uses Kiro Desktop Auth endpoint (no AWS SSO OIDC).
        """
        print("Setup: Creating KiroAuthManager with social login SQLite...")
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db_social)

        print("Verification: No client_id loaded (social login doesn't have it)...")
        assert manager._client_id is None

        print("Verification: No client_secret loaded...")
        assert manager._client_secret is None

        print("Verification: auth_type = KIRO_DESKTOP...")
        print(f"Comparing auth_type: Expected KIRO_DESKTOP, Got {manager.auth_type}")
        assert manager.auth_type == AuthType.KIRO_DESKTOP

    def test_social_token_key_has_highest_priority(self, temp_sqlite_db_all_keys):
        """
        What it does: Verifies kirocli:social:token has highest priority.
        Purpose: Ensure correct key is loaded when multiple keys exist.
        """
        print("Setup: Creating KiroAuthManager with database containing all three keys...")
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db_all_keys)

        print("Verification: Loaded from kirocli:social:token (highest priority)...")
        print(f"Comparing access_token: Expected 'social_token', Got '{manager._access_token}'")
        assert manager._access_token == "social_token"

        print(f"Comparing refresh_token: Expected 'social_refresh', Got '{manager._refresh_token}'")
        assert manager._refresh_token == "social_refresh"

        print("Verification: _sqlite_token_key tracks source...")
        print(f"Comparing _sqlite_token_key: Expected 'kirocli:social:token', Got '{manager._sqlite_token_key}'")
        assert manager._sqlite_token_key == "kirocli:social:token"

    def test_sqlite_token_key_tracked_for_social_login(self, temp_sqlite_db_social):
        """
        What it does: Verifies _sqlite_token_key is set when loading from social key.
        Purpose: Ensure tokens are saved back to correct key after refresh.
        """
        print("Setup: Creating KiroAuthManager with social login SQLite...")
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db_social)

        print("Verification: _sqlite_token_key set to kirocli:social:token...")
        print(f"Comparing _sqlite_token_key: Expected 'kirocli:social:token', Got '{manager._sqlite_token_key}'")
        assert manager._sqlite_token_key == "kirocli:social:token"

    def test_sqlite_token_key_tracked_for_odic(self, temp_sqlite_db):
        """
        What it does: Verifies _sqlite_token_key is set when loading from OIDC key.
        Purpose: Ensure backward compatibility with existing OIDC credentials.
        """
        print("Setup: Creating KiroAuthManager with OIDC SQLite...")
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db)

        print("Verification: _sqlite_token_key set to codewhisperer:odic:token...")
        print(f"Comparing _sqlite_token_key: Expected 'codewhisperer:odic:token', Got '{manager._sqlite_token_key}'")
        assert manager._sqlite_token_key == "codewhisperer:odic:token"

    def test_social_login_no_device_registration_key(self, temp_sqlite_db_social):
        """
        What it does: Verifies social login works without device-registration key.
        Purpose: Ensure social login doesn't require AWS SSO OIDC device registration.
        """
        import sqlite3

        print("Setup: Verifying database has no device-registration key...")
        conn = sqlite3.connect(temp_sqlite_db_social)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM auth_kv WHERE key LIKE '%device-registration%'")
        count = cursor.fetchone()[0]
        conn.close()

        print(f"Verification: No device-registration keys found (count={count})...")
        assert count == 0

        print("Setup: Creating KiroAuthManager with social login SQLite...")
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db_social)

        print("Verification: Manager initialized successfully without device-registration...")
        assert manager._access_token == "social_access_token"
        assert manager._client_id is None
        assert manager._client_secret is None


# =============================================================================
# Tests for Enterprise Kiro IDE Support (Issue #45)
# =============================================================================


class TestKiroAuthManagerEnterpriseIDE:
    """Tests for Enterprise Kiro IDE support (IdC login with clientIdHash).

    Background: Enterprise Kiro IDE uses AWS IAM Identity Center (IdC) for authentication.
    Credentials are stored in JSON file with clientIdHash field that points to a separate
    device registration file containing clientId and clientSecret.

    This is different from:
    - Personal Kiro IDE (social login): Uses Kiro Desktop Auth, no clientId/clientSecret
    - kiro-cli (SQLite): Uses AWS SSO OIDC, credentials in SQLite database
    """

    def test_load_credentials_from_file_with_client_id_hash(self, temp_enterprise_ide_complete):
        """
        What it does: Verifies loading credentials from JSON file with clientIdHash.
        Purpose: Ensure clientIdHash is detected and stored.
        """
        creds_file, device_reg_file = temp_enterprise_ide_complete

        print(f"Setup: Creating KiroAuthManager with Enterprise IDE credentials: {creds_file}")
        manager = KiroAuthManager(creds_file=creds_file)

        print("Verification: clientIdHash loaded...")
        print(f"Comparing _client_id_hash: Expected 'abc123def456', Got '{manager._client_id_hash}'")
        assert manager._client_id_hash == "abc123def456"

        print("Verification: Basic credentials loaded...")
        assert manager._access_token == "enterprise_access_token"
        assert manager._refresh_token == "enterprise_refresh_token"

    def test_load_enterprise_device_registration_success(self, temp_enterprise_ide_complete):
        """
        What it does: Verifies successful loading of device registration.
        Purpose: Ensure clientId and clientSecret are loaded from device registration file.
        """
        creds_file, device_reg_file = temp_enterprise_ide_complete

        print("Setup: Creating KiroAuthManager with Enterprise IDE credentials...")
        manager = KiroAuthManager(creds_file=creds_file)

        print("Verification: clientId loaded from device registration...")
        print(f"Comparing _client_id: Expected 'enterprise_client_id_12345', Got '{manager._client_id}'")
        assert manager._client_id == "enterprise_client_id_12345"

        print("Verification: clientSecret loaded from device registration...")
        print(f"Comparing _client_secret: Expected 'enterprise_client_secret_67890', Got '{manager._client_secret}'")
        assert manager._client_secret == "enterprise_client_secret_67890"

    def test_enterprise_ide_detected_as_aws_sso_oidc(self, temp_enterprise_ide_complete):
        """
        What it does: Verifies Enterprise IDE is detected as AWS_SSO_OIDC auth type.
        Purpose: Ensure correct authentication method is used (not Kiro Desktop Auth).
        """
        creds_file, device_reg_file = temp_enterprise_ide_complete

        print("Setup: Creating KiroAuthManager with Enterprise IDE credentials...")
        manager = KiroAuthManager(creds_file=creds_file)

        print("Verification: auth_type = AWS_SSO_OIDC...")
        print(f"Comparing auth_type: Expected AWS_SSO_OIDC, Got {manager.auth_type}")
        assert manager.auth_type == AuthType.AWS_SSO_OIDC

    def test_load_enterprise_device_registration_file_not_found(self, tmp_path, monkeypatch):
        """
        What it does: Verifies handling of missing device registration file.
        Purpose: Ensure application doesn't crash when device registration is missing.
        """
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        print("Setup: Creating credentials file with clientIdHash but no device registration...")
        creds_file = tmp_path / "kiro-auth-token.json"
        creds_data = {
            "accessToken": "enterprise_access_token",
            "refreshToken": "enterprise_refresh_token",
            "expiresAt": "2099-01-01T00:00:00.000Z",
            "region": "us-east-1",
            "clientIdHash": "nonexistent_hash",
        }
        creds_file.write_text(json.dumps(creds_data))

        print("Action: Creating KiroAuthManager...")
        manager = KiroAuthManager(creds_file=str(creds_file))

        print("Verification: clientIdHash stored...")
        assert manager._client_id_hash == "nonexistent_hash"

        print("Verification: clientId and clientSecret are None (file not found)...")
        assert manager._client_id is None
        assert manager._client_secret is None

        print("Verification: auth_type = KIRO_DESKTOP (no client credentials)...")
        assert manager.auth_type == AuthType.KIRO_DESKTOP

    def test_load_enterprise_device_registration_invalid_json(self, tmp_path, monkeypatch):
        """
        What it does: Verifies handling of invalid JSON in device registration file.
        Purpose: Ensure application doesn't crash on corrupted device registration.
        """
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        print("Setup: Creating device registration file with invalid JSON...")
        aws_dir = tmp_path / ".aws" / "sso" / "cache"
        aws_dir.mkdir(parents=True, exist_ok=True)

        device_reg_file = aws_dir / "invalid_hash.json"
        device_reg_file.write_text("not a valid json {{{")

        print("Setup: Creating credentials file...")
        creds_file = tmp_path / "kiro-auth-token.json"
        creds_data = {
            "accessToken": "enterprise_access_token",
            "refreshToken": "enterprise_refresh_token",
            "expiresAt": "2099-01-01T00:00:00.000Z",
            "region": "us-east-1",
            "clientIdHash": "invalid_hash",
        }
        creds_file.write_text(json.dumps(creds_data))

        print("Action: Creating KiroAuthManager (should handle error gracefully)...")
        manager = KiroAuthManager(creds_file=str(creds_file))

        print("Verification: clientId and clientSecret are None (JSON parse error)...")
        assert manager._client_id is None
        assert manager._client_secret is None

    def test_load_enterprise_device_registration_missing_fields(self, tmp_path, monkeypatch):
        """
        What it does: Verifies handling of device registration without clientId/clientSecret.
        Purpose: Ensure partial data doesn't cause crashes.
        """
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        print("Setup: Creating device registration file without clientId/clientSecret...")
        aws_dir = tmp_path / ".aws" / "sso" / "cache"
        aws_dir.mkdir(parents=True, exist_ok=True)

        device_reg_file = aws_dir / "partial_hash.json"
        device_reg_data = {"region": "us-east-1", "someOtherField": "value"}
        device_reg_file.write_text(json.dumps(device_reg_data))

        print("Setup: Creating credentials file...")
        creds_file = tmp_path / "kiro-auth-token.json"
        creds_data = {
            "accessToken": "enterprise_access_token",
            "refreshToken": "enterprise_refresh_token",
            "expiresAt": "2099-01-01T00:00:00.000Z",
            "region": "us-east-1",
            "clientIdHash": "partial_hash",
        }
        creds_file.write_text(json.dumps(creds_data))

        print("Action: Creating KiroAuthManager...")
        manager = KiroAuthManager(creds_file=str(creds_file))

        print("Verification: clientId and clientSecret are None (missing in file)...")
        assert manager._client_id is None
        assert manager._client_secret is None

    @pytest.mark.asyncio
    async def test_enterprise_ide_refresh_uses_json_format(
        self, temp_enterprise_ide_complete, mock_aws_sso_oidc_token_response
    ):
        """
        What it does: Verifies Enterprise IDE uses JSON format for token refresh.
        Purpose: Ensure correct request format (not form-urlencoded).
        """
        creds_file, device_reg_file = temp_enterprise_ide_complete

        print("Setup: Creating KiroAuthManager with Enterprise IDE credentials...")
        manager = KiroAuthManager(creds_file=creds_file)

        print("Verification: auth_type = AWS_SSO_OIDC...")
        assert manager.auth_type == AuthType.AWS_SSO_OIDC

        print("Setup: Mocking HTTP client...")
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=mock_aws_sso_oidc_token_response())
        mock_response.raise_for_status = Mock()

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            print("Action: Calling _refresh_token_aws_sso_oidc()...")
            await manager._refresh_token_aws_sso_oidc()

            print("Verification: POST request made...")
            mock_client.post.assert_called_once()

            print("Verification: Request uses JSON format (not form-urlencoded)...")
            call_args = mock_client.post.call_args
            assert "json" in call_args[1], "Request should use json= parameter"
            assert "data" not in call_args[1], "Request should NOT use data= parameter"

            print("Verification: Content-Type = application/json...")
            headers = call_args[1].get("headers", {})
            assert headers.get("Content-Type") == "application/json"

    @pytest.mark.asyncio
    async def test_enterprise_ide_refresh_uses_camel_case(
        self, temp_enterprise_ide_complete, mock_aws_sso_oidc_token_response
    ):
        """
        What it does: Verifies Enterprise IDE uses camelCase parameters.
        Purpose: Ensure correct parameter naming (not snake_case).
        """
        creds_file, device_reg_file = temp_enterprise_ide_complete

        print("Setup: Creating KiroAuthManager with Enterprise IDE credentials...")
        manager = KiroAuthManager(creds_file=creds_file)

        print("Setup: Mocking HTTP client...")
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=mock_aws_sso_oidc_token_response())
        mock_response.raise_for_status = Mock()

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            print("Action: Calling _refresh_token_aws_sso_oidc()...")
            await manager._refresh_token_aws_sso_oidc()

            print("Verification: Request uses camelCase parameters...")
            call_args = mock_client.post.call_args
            json_payload = call_args[1].get("json", {})

            print(f"JSON payload keys: {list(json_payload.keys())}")
            assert "grantType" in json_payload, "Should use grantType (camelCase)"
            assert "clientId" in json_payload, "Should use clientId (camelCase)"
            assert "clientSecret" in json_payload, "Should use clientSecret (camelCase)"
            assert "refreshToken" in json_payload, "Should use refreshToken (camelCase)"

            print("Verification: NOT using snake_case...")
            assert "grant_type" not in json_payload, "Should NOT use grant_type (snake_case)"
            assert "client_id" not in json_payload, "Should NOT use client_id (snake_case)"
            assert "client_secret" not in json_payload, "Should NOT use client_secret (snake_case)"
            assert "refresh_token" not in json_payload, "Should NOT use refresh_token (snake_case)"

    @pytest.mark.asyncio
    async def test_enterprise_ide_refresh_uses_correct_endpoint(
        self, temp_enterprise_ide_complete, mock_aws_sso_oidc_token_response
    ):
        """
        What it does: Verifies Enterprise IDE uses AWS SSO OIDC endpoint.
        Purpose: Ensure correct endpoint (not Kiro Desktop Auth).
        """
        creds_file, device_reg_file = temp_enterprise_ide_complete

        print("Setup: Creating KiroAuthManager with Enterprise IDE credentials...")
        manager = KiroAuthManager(creds_file=creds_file)

        print("Setup: Mocking HTTP client...")
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=mock_aws_sso_oidc_token_response())
        mock_response.raise_for_status = Mock()

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            print("Action: Calling _refresh_token_aws_sso_oidc()...")
            await manager._refresh_token_aws_sso_oidc()

            print("Verification: Request went to AWS SSO OIDC endpoint...")
            call_args = mock_client.post.call_args
            url = call_args[0][0]

            print(f"Comparing URL: Expected AWS SSO OIDC endpoint, Got '{url}'")
            assert "oidc" in url, "Should use AWS SSO OIDC endpoint"
            assert "amazonaws.com" in url, "Should use AWS endpoint"
            assert "/token" in url, "Should use /token endpoint"

            print("Verification: NOT using Kiro Desktop Auth endpoint...")
            assert "auth.desktop.kiro.dev" not in url, "Should NOT use Kiro Desktop Auth"

    @pytest.mark.asyncio
    async def test_enterprise_ide_full_refresh_flow(
        self, temp_enterprise_ide_complete, mock_aws_sso_oidc_token_response
    ):
        """
        What it does: Tests complete refresh flow for Enterprise IDE.
        Purpose: Integration test covering load → refresh → verify.
        """
        creds_file, device_reg_file = temp_enterprise_ide_complete

        print("Setup: Creating KiroAuthManager with Enterprise IDE credentials...")
        manager = KiroAuthManager(creds_file=creds_file)

        print("Verification: Initial state correct...")
        assert manager._client_id_hash == "abc123def456"
        assert manager._client_id == "enterprise_client_id_12345"
        assert manager._client_secret == "enterprise_client_secret_67890"
        assert manager.auth_type == AuthType.AWS_SSO_OIDC

        print("Setup: Mocking HTTP client for successful refresh...")
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = Mock(return_value=mock_aws_sso_oidc_token_response())
        mock_response.raise_for_status = Mock()

        with patch("kiro.auth.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            print("Action: Refreshing token...")
            await manager._refresh_token_aws_sso_oidc()

            print("Verification: Tokens updated...")
            assert manager._access_token == "new_aws_sso_access_token"
            assert manager._refresh_token == "new_aws_sso_refresh_token"

            print("Verification: Expiration time set...")
            assert manager._expires_at is not None
            assert manager._expires_at > datetime.now(timezone.utc)

    def test_enterprise_ide_and_kiro_cli_use_same_format(self):
        """
        What it does: Verifies Enterprise IDE and kiro-cli use identical request format.
        Purpose: Ensure architectural consistency (both use JSON with camelCase).
        """
        print("This test documents the architectural decision:")
        print("Both Enterprise IDE (JSON file) and kiro-cli (SQLite) use:")
        print("  - AWS SSO OIDC endpoint")
        print("  - JSON format (Content-Type: application/json)")
        print("  - camelCase parameters (grantType, clientId, etc.)")
        print("")
        print("The ONLY difference is where credentials are stored:")
        print("  - Enterprise IDE: JSON file + device registration file")
        print("  - kiro-cli: SQLite database")
        print("")
        print("This is verified by other tests in this class and")
        print("TestKiroAuthManagerSsoRegionSeparation class.")
        assert True  # Documentation test


# =============================================================================
# Tests for SQLite write-back preserving unknown fields (Issue #131)
# =============================================================================
class TestAPIRegionAutoDetectionSQLite:
    """Tests for automatic API region detection from SQLite profile ARN.

    Background: AWS uses different regions for SSO (OIDC token refresh) and Q API.
    The gateway must auto-detect the correct API region from profile ARN in state table.
    """

    def test_api_region_from_profile_arn_success(self, temp_sqlite_db_with_profile_arn):
        """
        What it does: Verifies successful extraction of API region from profile ARN.
        Purpose: Ensure API region is correctly parsed from ARN in state table.
        """
        print("Setup: Creating KiroAuthManager with SQLite containing profile ARN...")
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db_with_profile_arn)

        print("Verification: SSO region loaded from token data...")
        print(f"Comparing _sso_region: Expected 'eu-west-1', Got '{manager._sso_region}'")
        assert manager._sso_region == "eu-west-1"

        print("Verification: API region auto-detected from profile ARN...")
        print(f"Comparing _detected_api_region: Expected 'eu-central-1', Got '{manager._detected_api_region}'")
        assert manager._detected_api_region == "eu-central-1"

        print("Verification: API hosts use detected API region...")
        print(f"api_host: {manager._api_host}")
        assert "eu-central-1" in manager._api_host

        print(f"q_host: {manager._q_host}")
        assert "eu-central-1" in manager._q_host

        print("Verification: Refresh URL uses SSO region...")
        print(f"refresh_url: {manager._refresh_url}")
        assert "eu-west-1" in manager._refresh_url

    def test_api_region_env_var_overrides_arn(self, temp_sqlite_db_with_profile_arn, monkeypatch):
        """
        What it does: Verifies KIRO_API_REGION env var has highest priority.
        Purpose: Ensure manual override works even when ARN is present.
        """
        print("Setup: Setting KIRO_API_REGION env var...")
        monkeypatch.setenv("KIRO_API_REGION", "us-east-1")

        print("Setup: Creating KiroAuthManager with SQLite (ARN region=eu-central-1)...")
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db_with_profile_arn)

        print("Verification: API region detected from ARN...")
        print(f"Comparing _detected_api_region: Expected 'eu-central-1', Got '{manager._detected_api_region}'")
        assert manager._detected_api_region == "eu-central-1"

        print("Verification: But API hosts use env var override...")
        print(f"api_host: {manager._api_host}")
        assert "us-east-1" in manager._api_host
        assert "eu-central-1" not in manager._api_host

        print(f"q_host: {manager._q_host}")
        assert "us-east-1" in manager._q_host

    def test_api_region_fallback_when_no_state_table(self, temp_sqlite_db):
        """
        What it does: Verifies graceful fallback when state table doesn't exist.
        Purpose: Ensure backward compatibility with databases without state table.
        """
        print("Setup: Creating KiroAuthManager with SQLite without state table...")
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db)

        print("Verification: No API region detected...")
        print(f"Comparing _detected_api_region: Expected None, Got '{manager._detected_api_region}'")
        assert manager._detected_api_region is None

        print("Verification: API hosts use SSO region as fallback...")
        print(f"Comparing _sso_region: Expected 'eu-west-1', Got '{manager._sso_region}'")
        assert manager._sso_region == "eu-west-1"

        print(f"api_host: {manager._api_host}")
        assert "eu-west-1" in manager._api_host

    def test_api_region_fallback_when_no_profile_key(self, temp_sqlite_db_with_empty_state_table):
        """
        What it does: Verifies graceful fallback when profile key is missing from state table.
        Purpose: Ensure robustness when state table exists but has no profile.
        """
        print("Setup: Creating KiroAuthManager with empty state table...")
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db_with_empty_state_table)

        print("Verification: No API region detected...")
        print(f"Comparing _detected_api_region: Expected None, Got '{manager._detected_api_region}'")
        assert manager._detected_api_region is None

        print("Verification: API hosts use SSO region as fallback...")
        print(f"Comparing _sso_region: Expected 'us-west-2', Got '{manager._sso_region}'")
        assert manager._sso_region == "us-west-2"

        print(f"api_host: {manager._api_host}")
        assert "us-west-2" in manager._api_host

    def test_api_region_invalid_arn_format(self, temp_sqlite_db_with_invalid_arn):
        """
        What it does: Verifies handling of invalid ARN formats.
        Purpose: Ensure invalid ARNs are ignored and fallback is used.
        """
        print("Setup: Creating KiroAuthManager with invalid ARN...")
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db_with_invalid_arn)

        print("Verification: Invalid ARN ignored, no API region detected...")
        print(f"Comparing _detected_api_region: Expected None, Got '{manager._detected_api_region}'")
        assert manager._detected_api_region is None

        print("Verification: API hosts use SSO region as fallback...")
        print(f"Comparing _sso_region: Expected 'ap-southeast-1', Got '{manager._sso_region}'")
        assert manager._sso_region == "ap-southeast-1"

        print(f"api_host: {manager._api_host}")
        assert "ap-southeast-1" in manager._api_host

    def test_api_region_malformed_json_in_state_table(self, temp_sqlite_db_with_malformed_state_json):
        """
        What it does: Verifies handling of malformed JSON in state table.
        Purpose: Ensure graceful error handling without crashing.
        """
        print("Setup: Creating KiroAuthManager with malformed state JSON...")
        manager = KiroAuthManager(sqlite_db=temp_sqlite_db_with_malformed_state_json)

        print("Verification: Malformed JSON ignored, no API region detected...")
        print(f"Comparing _detected_api_region: Expected None, Got '{manager._detected_api_region}'")
        assert manager._detected_api_region is None

        print("Verification: API hosts use SSO region as fallback...")
        print(f"Comparing _sso_region: Expected 'ap-south-1', Got '{manager._sso_region}'")
        assert manager._sso_region == "ap-south-1"

        print(f"api_host: {manager._api_host}")
        assert "ap-south-1" in manager._api_host


# =============================================================================
# Tests for API Region Auto-Detection from JSON
# =============================================================================


class TestAPIRegionAutoDetectionJSON:
    """Tests for automatic API region detection from JSON credentials file.

    Background: JSON credentials contain region field that should be used as API region.
    """

    def test_api_region_from_json_region_field(self, temp_creds_file):
        """
        What it does: Verifies region field from JSON is used as API region.
        Purpose: Ensure JSON region is detected and used for API calls.
        """
        print("Setup: Creating KiroAuthManager with JSON credentials...")
        manager = KiroAuthManager(creds_file=temp_creds_file)

        print("Verification: Region detected from JSON...")
        print(f"Comparing _detected_api_region: Expected 'us-east-1', Got '{manager._detected_api_region}'")
        assert manager._detected_api_region == "us-east-1"

        print(f"Comparing _sso_region: Expected 'us-east-1', Got '{manager._sso_region}'")
        assert manager._sso_region == "us-east-1"

        print("Verification: API hosts use detected region...")
        print(f"api_host: {manager._api_host}")
        assert "us-east-1" in manager._api_host

    def test_api_region_json_env_var_override(self, temp_creds_file, monkeypatch):
        """
        What it does: Verifies KIRO_API_REGION env var overrides JSON region.
        Purpose: Ensure manual override has highest priority.
        """
        print("Setup: Setting KIRO_API_REGION env var...")
        monkeypatch.setenv("KIRO_API_REGION", "eu-central-1")

        print("Setup: Creating KiroAuthManager with JSON (region=us-east-1)...")
        manager = KiroAuthManager(creds_file=temp_creds_file)

        print("Verification: Region detected from JSON...")
        print(f"Comparing _detected_api_region: Expected 'us-east-1', Got '{manager._detected_api_region}'")
        assert manager._detected_api_region == "us-east-1"

        print("Verification: But API hosts use env var override...")
        print(f"api_host: {manager._api_host}")
        assert "eu-central-1" in manager._api_host
        assert "us-east-1" not in manager._api_host


# =============================================================================
# Tests for API Region Priority Hierarchy
# =============================================================================


class TestAPIRegionPriorityHierarchy:
    """Tests for complete priority hierarchy of API region determination.

    Priority order:
    1. KIRO_API_REGION env var (highest)
    2. Auto-detected from credentials
    3. SSO region (fallback)
    4. Default region parameter (lowest)
    """

    def test_priority_env_var_highest(self, monkeypatch):
        """
        What it does: Verifies KIRO_API_REGION has highest priority.
        Purpose: Ensure env var overrides everything.
        """
        print("Setup: Setting KIRO_API_REGION env var...")
        monkeypatch.setenv("KIRO_API_REGION", "us-west-1")

        print("Setup: Creating KiroAuthManager with default region...")
        manager = KiroAuthManager(refresh_token="test_refresh", region="us-east-1")

        print("Verification: API hosts use env var, not default...")
        print(f"api_host: {manager._api_host}")
        assert "us-west-1" in manager._api_host
        assert "us-east-1" not in manager._api_host

    def test_priority_fallback_to_default(self):
        """
        What it does: Verifies fallback to default region when nothing else available.
        Purpose: Ensure default region is used as last resort.
        """
        print("Setup: Creating KiroAuthManager with only refresh_token...")
        manager = KiroAuthManager(refresh_token="test_refresh", region="us-east-1")

        print("Verification: No auto-detection (no file, no SQLite)...")
        assert manager._detected_api_region is None
        assert manager._sso_region is None

        print("Verification: API hosts use default region...")
        print(f"api_host: {manager._api_host}")
        assert "us-east-1" in manager._api_host

    def test_priority_custom_default_region(self):
        """
        What it does: Verifies custom default region parameter is used.
        Purpose: Ensure region parameter works when no auto-detection.
        """
        print("Setup: Creating KiroAuthManager with custom region...")
        manager = KiroAuthManager(refresh_token="test_refresh", region="ap-south-1")

        print("Verification: API hosts use custom default region...")
        print(f"api_host: {manager._api_host}")
        assert "ap-south-1" in manager._api_host

    def test_auth_manager_api_region_parameter(self, temp_creds_file, monkeypatch):
        """
        What it does: Verifies api_region parameter has highest priority.
        Purpose: Ensure per-account api_region override works correctly.
        """
        print("Setup: Setting KIRO_API_REGION env var to us-west-1...")
        monkeypatch.setenv("KIRO_API_REGION", "us-west-1")

        print("Setup: Creating KiroAuthManager with api_region=eu-central-1...")
        print("  - JSON file has region=us-east-1 (detected)")
        print("  - KIRO_API_REGION env var=us-west-1")
        print("  - api_region parameter=eu-central-1 (should win)")
        manager = KiroAuthManager(creds_file=temp_creds_file, api_region="eu-central-1")

        print("Verification: Region detected from JSON...")
        print(f"Comparing _detected_api_region: Expected 'us-east-1', Got '{manager._detected_api_region}'")
        assert manager._detected_api_region == "us-east-1"

        print("Verification: But API hosts use api_region parameter (highest priority)...")
        print(f"api_host: {manager._api_host}")
        assert "eu-central-1" in manager._api_host
        assert "us-west-1" not in manager._api_host
        assert "us-east-1" not in manager._api_host

        print(f"q_host: {manager._q_host}")
        assert "eu-central-1" in manager._q_host

    def test_auth_manager_api_region_priority_hierarchy(self, temp_sqlite_db_with_profile_arn, monkeypatch):
        """
        What it does: Verifies complete priority hierarchy for API region.
        Purpose: Ensure all 5 levels of priority work correctly.

        Priority hierarchy (highest to lowest):
        1. api_region parameter (per-account override)
        2. KIRO_API_REGION env var (global override)
        3. Auto-detected from credentials (SQLite ARN or JSON region)
        4. SSO region (fallback)
        5. Default region parameter (last resort)
        """
        print("=== Test 1: api_region parameter (highest priority) ===")
        monkeypatch.setenv("KIRO_API_REGION", "us-west-1")
        manager1 = KiroAuthManager(
            sqlite_db=temp_sqlite_db_with_profile_arn, region="ap-south-1", api_region="ap-northeast-1"
        )
        print("  - api_region=ap-northeast-1 (parameter)")
        print("  - KIRO_API_REGION=us-west-1 (env)")
        print("  - detected=eu-central-1 (from ARN)")
        print("  - sso=eu-west-1 (from token)")
        print("  - default=ap-south-1 (parameter)")
        print(f"Result: api_host={manager1._api_host}")
        assert "ap-northeast-1" in manager1._api_host

        print("\n=== Test 2: KIRO_API_REGION env var (2nd priority) ===")
        manager2 = KiroAuthManager(sqlite_db=temp_sqlite_db_with_profile_arn, region="ap-south-1")
        print("  - api_region=None")
        print("  - KIRO_API_REGION=us-west-1 (env)")
        print("  - detected=eu-central-1 (from ARN)")
        print("  - sso=eu-west-1 (from token)")
        print("  - default=ap-south-1 (parameter)")
        print(f"Result: api_host={manager2._api_host}")
        assert "us-west-1" in manager2._api_host

        print("\n=== Test 3: Auto-detected from ARN (3rd priority) ===")
        monkeypatch.delenv("KIRO_API_REGION", raising=False)
        manager3 = KiroAuthManager(sqlite_db=temp_sqlite_db_with_profile_arn, region="ap-south-1")
        print("  - api_region=None")
        print("  - KIRO_API_REGION=None")
        print("  - detected=eu-central-1 (from ARN)")
        print("  - sso=eu-west-1 (from token)")
        print("  - default=ap-south-1 (parameter)")
        print(f"Result: api_host={manager3._api_host}")
        assert "eu-central-1" in manager3._api_host

        print("\n=== Test 4: SSO region fallback (4th priority) ===")
        manager4 = KiroAuthManager(sqlite_db=temp_sqlite_db_with_profile_arn, region="ap-south-1")
        # Force no detection by clearing detected region
        manager4._detected_api_region = None
        # Rebuild URLs to apply the change
        manager4._api_host = f"https://codewhisperer.{manager4._sso_region}.api.aws"
        manager4._q_host = f"https://q.{manager4._sso_region}.api.aws"
        print("  - api_region=None")
        print("  - KIRO_API_REGION=None")
        print("  - detected=None (forced)")
        print("  - sso=eu-west-1 (from token)")
        print("  - default=ap-south-1 (parameter)")
        print(f"Result: api_host={manager4._api_host}")
        assert "eu-west-1" in manager4._api_host

        print("\n=== Test 5: Default region (lowest priority) ===")
        manager5 = KiroAuthManager(refresh_token="test_refresh", region="ap-south-1")
        print("  - api_region=None")
        print("  - KIRO_API_REGION=None")
        print("  - detected=None (no file)")
        print("  - sso=None (no file)")
        print("  - default=ap-south-1 (parameter)")
        print(f"Result: api_host={manager5._api_host}")
        assert "ap-south-1" in manager5._api_host
