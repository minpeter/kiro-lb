"""Upstream account suspension is a permanent denial, not a token problem.

A suspended Builder ID answers ``403 AccessDeniedException`` with the message
"Your User ID (...) temporarily is suspended", while its refresh token stays
valid. Treating that as an expired token makes the gateway refresh
successfully, retry, get 403 again, and burn the whole retry budget; the
account then only ever reaches ``cooling_down``, which leaks 10% of routing
attempts into an account that can never recover on its own.
"""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro.account_errors import ErrorType, classify_error, is_suspension_error
from kiro.account_manager import Account, AccountManager, account_routing_state
from kiro.config import ACCOUNT_SUSPENSION_QUARANTINE
from kiro.kiro_errors import SUSPENSION_REASON, enhance_kiro_error

_SUSPENDED_BODY = {
    "message": (
        "Your User ID (94c89418-e061-709a-d0aa-0715ab00c707) temporarily is suspended. "
        "We've locked your account as a security precaution. To restore access, please "
        "contact our support team to verify your identity: "
        "https://app.kiro.dev/account/usage?support_form"
    ),
    "reason": None,
}

# The runtime host words it differently and supplies a real reason code; both
# hosts must be recognized because a Builder ID account can land on either.
_SUSPENDED_BODY_RUNTIME = {
    "message": (
        "Your User ID is temporarily suspended. We detected unusual user activity and locked it "
        "as a security precaution. To restore access, please contact our support team to verify "
        "your identity: https://support.aws.amazon.com/#/contacts/kiro"
    ),
    "reason": "TEMPORARILY_SUSPENDED",
}


class TestSuspensionDetection:
    def test_suspension_body_is_recognized(self):
        assert is_suspension_error(403, _SUSPENDED_BODY["message"]) is True

    def test_runtime_host_reason_code_is_recognized(self):
        assert is_suspension_error(403, _SUSPENDED_BODY_RUNTIME["message"], "TEMPORARILY_SUSPENDED") is True

    def test_runtime_host_wording_alone_is_recognized(self):
        assert is_suspension_error(403, _SUSPENDED_BODY_RUNTIME["message"]) is True

    def test_enhance_kiro_error_labels_the_runtime_suspension(self):
        info = enhance_kiro_error(_SUSPENDED_BODY_RUNTIME)

        assert info.reason == SUSPENSION_REASON
        assert "suspended" in info.user_message.lower()

    def test_enhance_kiro_error_labels_the_suspension(self):
        info = enhance_kiro_error(_SUSPENDED_BODY)

        assert info.reason == SUSPENSION_REASON
        assert "suspended" in info.user_message.lower()

    def test_plain_403_is_not_a_suspension(self):
        assert is_suspension_error(403, "Improperly formed request.") is False

    def test_suspension_wording_on_another_status_is_not_a_suspension(self):
        # Only an authorization refusal carries this meaning; the same words in a
        # 400 body would be a payload echo, not an account verdict.
        assert is_suspension_error(400, _SUSPENDED_BODY["message"]) is False

    def test_missing_message_is_not_a_suspension(self):
        assert is_suspension_error(403, None) is False

    def test_suspension_classifies_as_recoverable_for_failover(self):
        assert classify_error(403, SUSPENSION_REASON) == ErrorType.RECOVERABLE

    def test_ordinary_403_still_classifies_as_recoverable(self):
        assert classify_error(403, None) == ErrorType.RECOVERABLE


def _account(account_id: str = "/creds/banned.json") -> Account:
    account = Account(id=account_id)
    account.auth_manager = MagicMock()
    return account


class TestSuspendedAccountState:
    def test_routing_state_reports_suspended(self):
        account = _account()
        account.suspended_until = time.time() + 3600

        state, eligible_in = account_routing_state(account)

        assert state == "suspended"
        assert 3500 < eligible_in <= 3600

    def test_suspension_outranks_cooldown_and_quota(self):
        account = _account()
        account.suspended_until = time.time() + 3600
        account.quota_exhausted_until = time.time() + 60
        account.failures = 5
        account.last_failure_time = time.time()

        state, _ = account_routing_state(account)

        assert state == "suspended"

    def test_expired_suspension_returns_to_available(self):
        account = _account()
        account.suspended_until = time.time() - 1

        state, eligible_in = account_routing_state(account)

        assert state == "available"
        assert eligible_in == 0


@pytest.fixture
def manager(tmp_path):
    mgr = AccountManager()
    mgr._save_state = AsyncMock()
    return mgr


class TestSuspensionQuarantine:
    def test_report_failure_quarantines_a_suspended_account(self, manager):
        import asyncio

        account = _account()
        manager._accounts[account.id] = account

        asyncio.run(
            manager.report_failure(account.id, "claude-sonnet-4.5", ErrorType.RECOVERABLE, 403, SUSPENSION_REASON)
        )

        remaining = account.suspended_until - time.time()
        assert remaining == pytest.approx(ACCOUNT_SUSPENSION_QUARANTINE, abs=5)
        # The Circuit Breaker must stay untouched: the exclusion is already
        # total, and inflating failures only lengthens an unrelated backoff.
        assert account.failures == 0
        assert account.stats.failed_requests == 1

    def test_legacy_host_suspension_quarantines_without_a_reason_code(self, manager):
        """The q.* host sends reason=null and states the verdict in the message.

        Matching on the reason code alone left these accounts in the rotation
        answering 403 to every request, which is the exact failure the
        quarantine exists to prevent.
        """
        import asyncio

        account = _account()
        manager._accounts[account.id] = account

        asyncio.run(
            manager.report_failure(
                account.id, "claude-sonnet-4.5", ErrorType.RECOVERABLE, 403, None, _SUSPENDED_BODY["message"]
            )
        )

        remaining = account.suspended_until - time.time()
        assert remaining == pytest.approx(ACCOUNT_SUSPENSION_QUARANTINE, abs=5)
        assert account.failures == 0

    def test_runtime_host_suspension_quarantines_from_the_message_too(self, manager):
        import asyncio

        account = _account()
        manager._accounts[account.id] = account

        asyncio.run(
            manager.report_failure(
                account.id, "claude-sonnet-4.5", ErrorType.RECOVERABLE, 403, None, _SUSPENDED_BODY_RUNTIME["message"]
            )
        )

        assert account.suspended_until > time.time()
        assert account.failures == 0

    def test_a_plain_403_still_drives_the_circuit_breaker(self, manager):
        """Only a suspension verdict may quarantine; an ordinary 403 must not."""
        import asyncio

        account = _account()
        manager._accounts[account.id] = account

        asyncio.run(
            manager.report_failure(
                account.id,
                "claude-sonnet-4.5",
                ErrorType.RECOVERABLE,
                403,
                None,
                "The security token included in the request is expired",
            )
        )

        assert account.suspended_until == 0.0
        assert account.failures == 1

    def test_suspension_wording_in_a_non_403_is_not_a_verdict(self, manager):
        """A 400 echoing the payload says nothing about the account itself."""
        import asyncio

        account = _account()
        manager._accounts[account.id] = account

        asyncio.run(
            manager.report_failure(
                account.id, "claude-sonnet-4.5", ErrorType.RECOVERABLE, 400, None, _SUSPENDED_BODY["message"]
            )
        )

        assert account.suspended_until == 0.0
        assert account.failures == 1

    def test_suspended_account_is_never_selected(self, manager):
        import asyncio

        banned = _account("/creds/banned.json")
        banned.suspended_until = time.time() + 3600
        healthy = _account("/creds/healthy.json")
        healthy.models_cached_at = time.time()
        manager._accounts[banned.id] = banned
        manager._accounts[healthy.id] = healthy

        # Probabilistic retry must not reach a suspended account, so repeat
        # enough times that a 10% leak would be overwhelmingly likely to show.
        picks = {asyncio.run(manager.get_next_account("claude-sonnet-4.5")).id for _ in range(200)}

        assert picks == {healthy.id}

    def test_pool_of_only_suspended_accounts_yields_nothing(self, manager):
        import asyncio

        first = _account("/creds/a.json")
        second = _account("/creds/b.json")
        for account in (first, second):
            account.suspended_until = time.time() + 3600
            manager._accounts[account.id] = account

        assert asyncio.run(manager.get_next_account("claude-sonnet-4.5")) is None

    def test_success_clears_a_stale_suspension(self, manager):
        import asyncio

        account = _account()
        account.suspended_until = time.time() + 3600
        manager._accounts[account.id] = account

        asyncio.run(manager.report_success(account.id, "claude-sonnet-4.5"))

        assert account.suspended_until == 0.0


class TestSuspensionDetectedAtInitialization:
    """A locked account must leave the pool before it costs a client request.

    ListAvailableModels is the first upstream call an account makes. It answers
    403 for a suspended account, and that non-200 used to be swallowed by the
    FALLBACK_MODELS path: the account came up advertising all 19 models and
    collected traffic it could only answer 403 to.
    """

    @staticmethod
    def _manager_with_json_account(tmp_path):
        import json as json_module

        creds = tmp_path / "account.json"
        # clientId/clientSecret select AWS SSO OIDC, which resolves to the legacy
        # q.* host. The runtime host skips ListAvailableModels entirely
        # (_is_runtime_endpoint), so it could never surface a suspension here.
        creds.write_text(
            json_module.dumps(
                {
                    "refreshToken": "t",
                    "accessToken": "a",
                    "expiresAt": "2099-01-01T00:00:00.000Z",
                    "clientId": "client-id",
                    "clientSecret": "client-secret",
                    "region": "us-east-1",
                }
            )
        )
        pool = tmp_path / "credentials.json"
        entries = [{"type": "json", "path": str(creds)}]
        pool.write_text(json_module.dumps(entries))
        from tests.conftest import seed_account_sources

        seed_account_sources(entries)
        mgr = AccountManager()
        mgr._save_state = AsyncMock()
        return mgr, str(creds.resolve())

    def _initialize_with_response(self, tmp_path, status_code, body):
        import asyncio
        import json as json_module
        from unittest.mock import patch

        manager, account_id = self._manager_with_json_account(tmp_path)
        asyncio.run(manager.load_credentials())

        response = MagicMock()
        response.status_code = status_code
        response.text = json_module.dumps(body)
        response.json.return_value = body

        with patch("kiro.account_manager.KiroHttpClient") as http_class:
            client = AsyncMock()
            client.request_with_retry = AsyncMock(return_value=response)
            client.close = AsyncMock()
            http_class.return_value = client
            ok = asyncio.run(manager._initialize_account(account_id))
        return manager, account_id, ok

    def test_legacy_host_403_quarantines_instead_of_falling_back(self, tmp_path):
        manager, account_id, _ = self._initialize_with_response(tmp_path, 403, _SUSPENDED_BODY)

        account = manager._accounts[account_id]
        remaining = account.suspended_until - time.time()
        assert remaining == pytest.approx(ACCOUNT_SUSPENSION_QUARANTINE, abs=5)

    def test_runtime_host_403_quarantines_too(self, tmp_path):
        manager, account_id, _ = self._initialize_with_response(tmp_path, 403, _SUSPENDED_BODY_RUNTIME)

        assert manager._accounts[account_id].suspended_until > time.time()

    def test_a_quarantined_account_advertises_no_models(self, tmp_path):
        """Falling back to the static list is what made a locked account look healthy."""
        manager, account_id, _ = self._initialize_with_response(tmp_path, 403, _SUSPENDED_BODY)

        resolver = manager._accounts[account_id].model_resolver
        advertised = set(resolver.get_available_models()) if resolver else set()
        assert "claude-opus-5" not in advertised

    def test_a_quarantined_account_is_not_a_routing_target(self, tmp_path):
        """With a healthy peer present, the locked account must never be picked.

        A second account is required for this assertion: the single-account path
        deliberately bypasses every exclusion so the operator sees the real
        upstream error instead of a generic "no account available".
        """
        import asyncio

        manager, account_id, _ = self._initialize_with_response(tmp_path, 403, _SUSPENDED_BODY)
        healthy = _account("/creds/healthy.json")
        healthy.models_cached_at = time.time()
        manager._accounts[healthy.id] = healthy

        picks = {asyncio.run(manager.get_next_account("claude-sonnet-4.5")).id for _ in range(50)}

        assert picks == {healthy.id}

    def test_an_unrelated_failure_still_falls_back(self, tmp_path):
        """Only a suspension may empty the model list; a network blip must not."""
        import asyncio
        from unittest.mock import patch

        manager, account_id = self._manager_with_json_account(tmp_path)
        asyncio.run(manager.load_credentials())

        with patch("kiro.account_manager.KiroHttpClient") as http_class:
            client = AsyncMock()
            client.request_with_retry = AsyncMock(side_effect=Exception("Network error"))
            client.close = AsyncMock()
            http_class.return_value = client
            ok = asyncio.run(manager._initialize_account(account_id))

        assert ok is True
        assert manager._accounts[account_id].suspended_until == 0.0


class TestSuspensionDetectedOnTtlRefresh:
    """A suspension can also arrive after an account is already healthy.

    The model cache expires on ACCOUNT_CACHE_TTL and _refresh_account_models
    re-lists. That call answers 403 once the account is locked, and treating any
    non-200 as "keep the stale cache" left the account advertising every model
    for as long as the process lived.
    """

    @staticmethod
    def _healthy_account(account_id: str = "/creds/locked-later.json") -> Account:
        from kiro.cache import ModelInfoCache
        from kiro.model_resolver import ModelResolver

        account = Account(id=account_id)
        auth = MagicMock()
        # A plain MagicMock q_host would make _is_runtime_endpoint short-circuit
        # the refresh, so the legacy host is set explicitly.
        auth.q_host = "https://q.us-east-1.amazonaws.com"
        auth.profile_arn = None
        account.auth_manager = auth
        account.model_cache = ModelInfoCache()
        account.model_resolver = ModelResolver(cache=account.model_cache, hidden_models={}, aliases={})
        account.models_cached_at = time.time()
        return account

    def _refresh_with_response(self, manager, status_code, body):
        import asyncio
        import json as json_module
        from unittest.mock import patch

        response = MagicMock()
        response.status_code = status_code
        response.text = json_module.dumps(body)
        response.json.return_value = body

        with patch("kiro.account_manager.KiroHttpClient") as http_class:
            client = AsyncMock()
            client.request_with_retry = AsyncMock(return_value=response)
            client.close = AsyncMock()
            http_class.return_value = client
            asyncio.run(manager._refresh_account_models(next(iter(manager._accounts))))

    def test_refresh_403_quarantines_the_account(self, manager):
        account = self._healthy_account()
        manager._accounts[account.id] = account

        self._refresh_with_response(manager, 403, _SUSPENDED_BODY)

        remaining = account.suspended_until - time.time()
        assert remaining == pytest.approx(ACCOUNT_SUSPENSION_QUARANTINE, abs=5)

    def test_refresh_403_stops_the_account_being_routed_to(self, manager):
        import asyncio

        account = self._healthy_account()
        manager._accounts[account.id] = account
        self._refresh_with_response(manager, 403, _SUSPENDED_BODY)

        healthy = _account("/creds/healthy.json")
        healthy.models_cached_at = time.time()
        manager._accounts[healthy.id] = healthy

        picks = {asyncio.run(manager.get_next_account("claude-sonnet-4.5")).id for _ in range(50)}

        assert picks == {healthy.id}

    def test_an_unrelated_refresh_failure_is_not_a_suspension(self, manager):
        """A 500 says nothing about the account; the stale cache stays usable."""
        account = self._healthy_account()
        manager._accounts[account.id] = account

        self._refresh_with_response(manager, 500, {"message": "internal", "reason": None})

        assert account.suspended_until == 0.0


class TestSuspensionPersistence:
    def test_suspension_survives_a_restart(self, tmp_path):
        import asyncio

        from kiro.store import load_runtime_state

        mgr = AccountManager()
        account = _account()
        account.suspended_until = time.time() + 3600
        mgr._accounts[account.id] = account
        asyncio.run(mgr._save_state())

        saved = load_runtime_state()
        assert saved is not None
        assert saved["accounts"][account.id]["suspended_until"] == pytest.approx(account.suspended_until)

        reloaded = AccountManager()
        reloaded._accounts[account.id] = _account()
        asyncio.run(reloaded.load_state())
        assert reloaded._accounts[account.id].suspended_until == pytest.approx(account.suspended_until)
