# -*- coding: utf-8 -*-
"""A refresh token the auth host rejects is a dead account, not a 500.

The refresh endpoint answers ``401 {"message": "Bad credentials"}`` once a token
has been revoked. ``httpx`` raises ``HTTPStatusError`` for that, which is neither
``RequestError`` nor ``TimeoutException`` - so it matched none of the handlers in
``request_with_retry``, escaped ``get_access_token``, sailed past the routes'
``except HTTPException`` and landed in the bare ``except Exception``. The caller
got HTTP 500, ``report_failure`` was never called, and the permanently dead
account kept its place in the rotation for every subsequent request.

These tests pin the whole chain: the auth layer raises a typed error, the pool
quarantines the account ahead of every other exclusion, and a success still
clears the verdict.
"""

import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from kiro.account_errors import (
    CredentialDeadError,
    ErrorType,
    classify_error,
    is_credential_dead_status,
)
from kiro.account_manager import Account, AccountManager, account_routing_state
from kiro.auth import KiroAuthManager
from kiro.config import ACCOUNT_AUTH_DEAD_QUARANTINE

#: The exact upstream shape, verified against the live endpoint.
_DEAD_CREDENTIAL_BODY = {"message": "Bad credentials"}


def _status_error(status_code: int, url: str = "https://prod.us-east-1.auth.desktop.kiro.dev/refreshToken"):
    request = httpx.Request("POST", url)
    response = httpx.Response(status_code, request=request, json=_DEAD_CREDENTIAL_BODY)
    return httpx.HTTPStatusError(f"Client error '{status_code}'", request=request, response=response)


@pytest.fixture
def manager() -> AccountManager:
    instance = AccountManager.__new__(AccountManager)
    instance._accounts = {}
    instance._model_to_accounts = {}
    instance._credentials_config = []
    instance._current_account_index = 0
    instance._dirty = False
    instance._rate_observations = []
    instance._unsaved_rate_observations = []
    instance._model_refreshes = {}
    instance._lock = __import__("asyncio").Lock()
    return instance


class TestCredentialDeadStatuses:
    """Only the token endpoint's terminal refusals count."""

    def test_401_is_a_dead_credential(self):
        assert is_credential_dead_status(401) is True

    def test_400_is_a_dead_credential(self):
        # Reached only after the raw-source reload already failed, so by then no
        # stored copy of the token works.
        assert is_credential_dead_status(400) is True

    def test_transient_auth_host_failure_is_not_a_dead_credential(self):
        # A 5xx must keep its retry meaning; parking the account on it would turn
        # an auth-host blip into a day-long exclusion.
        assert is_credential_dead_status(500) is False
        assert is_credential_dead_status(503) is False

    def test_403_is_not_a_dead_credential(self):
        # 403 is the data plane's suspension verdict, handled by is_suspension_error.
        assert is_credential_dead_status(403) is False


class TestCredentialDeadErrorShape:
    def test_error_carries_the_status_for_the_route_to_report(self):
        error = CredentialDeadError("refresh_token_abc", 401)

        assert error.status_code == 401
        assert "refresh_token_abc" in str(error)

    def test_error_message_omits_the_refresh_url(self):
        """The URL is the reason the dashboard cell blew up; keep it out.

        httpx interpolates the endpoint plus an MDN link, producing 188 characters
        across two lines. None of it is account-specific.
        """
        error = CredentialDeadError("refresh_token_abc", 401)

        assert "auth.desktop.kiro.dev" not in str(error)
        assert "developer.mozilla.org" not in str(error)
        assert "\n" not in str(error)


class TestAuthManagerTranslatesTerminalRefusals:
    def test_401_becomes_a_credential_dead_error(self):
        auth = KiroAuthManager(refresh_token="dead-token", profile_arn="arn:aws:codewhisperer:us-east-1:1:profile/x")

        translated = auth._credential_dead_error(_status_error(401))

        assert isinstance(translated, CredentialDeadError)
        assert translated.status_code == 401

    def test_500_is_handed_back_unchanged(self):
        auth = KiroAuthManager(refresh_token="tok", profile_arn="arn:aws:codewhisperer:us-east-1:1:profile/x")
        original = _status_error(503)

        assert auth._credential_dead_error(original) is original

    @pytest.mark.asyncio
    async def test_get_access_token_raises_the_typed_error(self):
        """The bug in one assertion: the raw HTTPStatusError must not escape."""
        auth = KiroAuthManager(refresh_token="dead-token", profile_arn="arn:aws:codewhisperer:us-east-1:1:profile/x")
        auth._refresh_with_store_lease = AsyncMock(side_effect=_status_error(401))

        with pytest.raises(CredentialDeadError):
            await auth.get_access_token()

    @pytest.mark.asyncio
    async def test_force_refresh_raises_the_typed_error(self):
        auth = KiroAuthManager(refresh_token="dead-token", profile_arn="arn:aws:codewhisperer:us-east-1:1:profile/x")
        auth._refresh_with_store_lease = AsyncMock(side_effect=_status_error(401))

        with pytest.raises(CredentialDeadError):
            await auth.force_refresh()

    @pytest.mark.asyncio
    async def test_a_transient_auth_host_error_still_propagates_as_is(self):
        auth = KiroAuthManager(refresh_token="tok", profile_arn="arn:aws:codewhisperer:us-east-1:1:profile/x")
        auth._refresh_with_store_lease = AsyncMock(side_effect=_status_error(503))

        with pytest.raises(httpx.HTTPStatusError):
            await auth.get_access_token()


class TestAuthDeadRoutingState:
    def test_routing_state_reports_auth_dead(self):
        account = Account(id="a", auth_dead_until=time.time() + 3600)

        state, seconds = account_routing_state(account)

        assert state == "auth_dead"
        assert seconds > 0

    def test_auth_dead_outranks_every_other_exclusion(self):
        """Reported first because it is the only one with nothing left to ask.

        A suspended account still has a reachable upstream verdict; this one
        cannot even obtain a token, so the other labels would misdirect the
        operator to the pool instead of the credential.
        """
        now = time.time()
        account = Account(
            id="a",
            auth_dead_until=now + 3600,
            suspended_until=now + 3600,
            quota_exhausted_until=now + 3600,
            rate_limited_until=now + 3600,
            failures=5,
            last_failure_time=now,
        )

        assert account_routing_state(account, now)[0] == "auth_dead"

    def test_expired_verdict_stops_excluding(self):
        """The window bounds how long a stale verdict is trusted, nothing more."""
        account = Account(id="a", auth_manager=MagicMock(), auth_dead_until=time.time() - 1)

        assert account_routing_state(account)[0] == "available"


class TestCredentialDeadQuarantine:
    @pytest.mark.asyncio
    async def test_report_credential_dead_parks_the_account(self, manager):
        manager._accounts["a"] = Account(id="a")

        await manager.report_credential_dead("a", 401)

        account = manager._accounts["a"]
        assert account.auth_dead_until > time.time()
        assert account.auth_dead_until <= time.time() + ACCOUNT_AUTH_DEAD_QUARANTINE + 1
        assert account_routing_state(account)[0] == "auth_dead"

    @pytest.mark.asyncio
    async def test_quarantine_leaves_the_circuit_breaker_alone(self, manager):
        """Failures stay at zero: the account is already fully excluded.

        Incrementing them would add an unrelated backoff and, worse, hand the
        account back to the 10% probabilistic retry once that backoff expired -
        spending real requests to re-prove a credential is dead.
        """
        manager._accounts["a"] = Account(id="a")

        await manager.report_credential_dead("a", 401)

        assert manager._accounts["a"].failures == 0

    @pytest.mark.asyncio
    async def test_the_failure_is_still_counted_in_stats(self, manager):
        manager._accounts["a"] = Account(id="a")

        await manager.report_credential_dead("a", 401)

        stats = manager._accounts["a"].stats
        assert stats.total_requests == 1
        assert stats.failed_requests == 1

    @pytest.mark.asyncio
    async def test_an_unknown_account_is_ignored(self, manager):
        await manager.report_credential_dead("nope", 401)

        assert manager._accounts == {}

    @pytest.mark.asyncio
    async def test_a_dead_account_is_never_selected(self, manager):
        manager._accounts["dead"] = Account(id="dead", auth_dead_until=time.time() + 3600)
        healthy = Account(id="healthy", auth_manager=MagicMock(), model_resolver=MagicMock())
        healthy.model_resolver.get_available_models.return_value = ["m"]
        manager._accounts["healthy"] = healthy

        for _ in range(20):
            chosen = await manager.get_next_account("m")
            assert chosen is not None and chosen.id == "healthy"

    @pytest.mark.asyncio
    async def test_success_clears_a_stale_verdict(self, manager):
        """A served request outranks any stored prediction of death.

        Re-registering the account, or another process writing a fresh token,
        makes the credential work again; the verdict must not outlive that.
        """
        manager._accounts["a"] = Account(id="a", auth_manager=MagicMock(), auth_dead_until=time.time() + 3600)

        await manager.report_success("a", "m")

        assert manager._accounts["a"].auth_dead_until == 0.0
        assert account_routing_state(manager._accounts["a"])[0] == "available"

    @pytest.mark.asyncio
    async def test_pool_state_names_the_remedy(self, manager):
        manager._accounts["a"] = Account(id="a", auth_dead_until=time.time() + 3600)

        described = manager.describe_pool_state()

        assert "re-login required" in described
        # Must not be reported as available, which is what sent an operator to
        # debug the pool instead of the credential.
        assert "available" not in described


class TestAuthDeadPersistence:
    def test_the_verdict_survives_a_restart(self, manager):
        """Persisted on purpose: a re-tested dead credential is a wasted request.

        Unlike a rate-limit window, this condition is longer than any restart.
        """
        manager._accounts["a"] = Account(id="a", auth_dead_until=1_800_000_000.0)

        document = manager._state_document()

        assert document["accounts"]["a"]["auth_dead_until"] == 1_800_000_000.0

    @pytest.mark.asyncio
    async def test_a_document_without_the_field_loads_as_zero(self, manager, monkeypatch):
        """A pre-upgrade state document must still load, not KeyError.

        Existing deployments have runtime state written before this field existed,
        and the blue/green handoff reads it on every start.
        """
        manager._accounts["a"] = Account(id="a", auth_dead_until=999.0)
        legacy = {"current_account_index": 0, "accounts": {"a": {"failures": 0}}, "model_to_accounts": {}}
        monkeypatch.setattr("kiro.store.load_runtime_state", lambda: legacy)
        manager._seed_quota_headroom = lambda: None

        await manager.load_state()

        assert manager._accounts["a"].auth_dead_until == 0.0

    @pytest.mark.asyncio
    async def test_a_persisted_verdict_is_restored(self, manager, monkeypatch):
        manager._accounts["a"] = Account(id="a")
        stored = {
            "current_account_index": 0,
            "accounts": {"a": {"auth_dead_until": 1_800_000_000.0}},
            "model_to_accounts": {},
        }
        monkeypatch.setattr("kiro.store.load_runtime_state", lambda: stored)
        manager._seed_quota_headroom = lambda: None

        await manager.load_state()

        assert manager._accounts["a"].auth_dead_until == 1_800_000_000.0


class TestDataPlaneClassificationIsUnchanged:
    """The new state must not disturb the existing error taxonomy."""

    def test_402_and_429_still_fail_over(self):
        assert classify_error(402, "MONTHLY_REQUEST_COUNT") is ErrorType.RECOVERABLE
        assert classify_error(429, None) is ErrorType.RECOVERABLE

    def test_a_malformed_request_is_still_fatal(self):
        assert classify_error(400, None) is ErrorType.FATAL
