# -*- coding: utf-8 -*-
"""
Unified Account System for Kiro Gateway.

Manages multiple Kiro accounts with intelligent failover, sticky behavior,
and circuit breaker pattern for reliability.

Key features:
- Lazy initialization (only first working account at startup)
- Sticky behavior (prefer successful account)
- Circuit breaker with exponential backoff
- Probabilistic retry for "dead" accounts
- TTL-based model cache refresh (only when using account)
- Atomic state persistence
"""

import asyncio
import hashlib
import json
import math
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
from loguru import logger

from kiro.account_errors import CredentialDeadError, ErrorType
from kiro.auth import AuthType, KiroAuthManager
from kiro.cache import ModelInfoCache
from kiro.config import (
    ACCOUNT_AUTH_DEAD_QUARANTINE,
    ACCOUNT_CACHE_TTL,
    ACCOUNT_DEPLETED_QUOTA_WEIGHT,
    ACCOUNT_MAX_BACKOFF_MULTIPLIER,
    ACCOUNT_PROBABILISTIC_RETRY_CHANCE,
    ACCOUNT_QUOTA_QUARANTINE,
    ACCOUNT_QUOTA_QUARANTINE_MAX,
    ACCOUNT_QUOTA_RESET_MARGIN,
    ACCOUNT_QUOTA_WEIGHTED_ROUTING,
    ACCOUNT_RATE_LIMIT_COOLDOWN,
    ACCOUNT_RECOVERY_TIMEOUT,
    ACCOUNT_SUSPENSION_QUARANTINE,
    ACCOUNT_UNKNOWN_QUOTA_WEIGHT,
    FALLBACK_MODELS,
    HIDDEN_FROM_LIST,
    HIDDEN_MODELS,
    MINIMUM_ROUTING_WEIGHT,
    MODEL_ALIASES,
    RATE_ESTIMATE_WINDOW_SECONDS,
    RATE_WINDOW_SECONDS,
    STATE_SAVE_INTERVAL_SECONDS,
)
from kiro.http_client import KiroHttpClient
from kiro.kiro_errors import is_suspension_error
from kiro.model_resolver import ModelResolver, normalize_model_name


def _is_runtime_endpoint(auth_manager: KiroAuthManager) -> bool:
    """
    Check if auth manager uses runtime endpoint that doesn't provide /ListAvailableModels.

    Runtime endpoint pattern: https://runtime.{region}.kiro.dev
    Old endpoint pattern: https://q.{region}.amazonaws.com

    Runtime endpoint does not provide /ListAvailableModels API (AWS limitation).

    Args:
        auth_manager: KiroAuthManager instance

    Returns:
        True if using runtime endpoint, False otherwise

    Examples:
        >>> auth_manager.api_host = "https://runtime.us-east-1.kiro.dev"
        >>> _is_runtime_endpoint(auth_manager)
        True
        >>> auth_manager.api_host = "https://runtime.eu-central-1.kiro.dev"
        >>> _is_runtime_endpoint(auth_manager)
        True
        >>> auth_manager.api_host = "https://q.us-east-1.amazonaws.com"
        >>> _is_runtime_endpoint(auth_manager)
        False
    """
    return "://runtime." in auth_manager.api_host


def account_label(account_id: str) -> str:
    """
    Build a stable, non-secret label for an account.

    Account IDs are credential file paths (or refresh-token hashes), so they are
    never exposed to API clients. The label is the same short digest the
    dashboard shows for an account, which lets an operator match a client-facing
    error against a row in the accounts view.

    Args:
        account_id: Internal account ID

    Returns:
        12-character hex digest of the account ID
    """
    return hashlib.sha256(account_id.encode()).hexdigest()[:12]


def account_routing_state(account: "Account", now: Optional[float] = None) -> Tuple[str, int]:
    """
    Classify why an account is or is not a routing target right now.

    Single source of truth for both the client-facing 503 diagnostics and the
    dashboard: the failure counter alone no longer tells the story, because a
    rate limit and a quota exhaustion deliberately leave it untouched.

    Args:
        account: Account to classify
        now: Reference timestamp (defaults to the current time)

    Returns:
        Tuple of (state, seconds_until_eligible). State is one of
        "auth_dead", "suspended", "quota_exhausted", "quota_depleted",
        "rate_limited", "cooling_down", "uninitialized", or "available"; the
        second element is 0 when nothing is pending.
    """
    now = time.time() if now is None else now

    # A rejected credential outranks every other exclusion, suspension included:
    # a suspension is a verdict about the account that support can lift, while
    # this account cannot even obtain a token, so nothing downstream is reachable
    # to ask for a newer verdict.
    auth_dead_remaining = account.auth_dead_until - now
    if auth_dead_remaining > 0:
        return ("auth_dead", int(auth_dead_remaining))

    # A suspension outranks the remaining exclusions: they describe a condition
    # that clears on its own, this one does not.
    suspension_remaining = account.suspended_until - now
    if suspension_remaining > 0:
        return ("suspended", int(suspension_remaining))

    quota_remaining = account.quota_exhausted_until - now
    if quota_remaining > 0:
        return ("quota_exhausted", int(quota_remaining))

    rate_limit_remaining = account.rate_limited_until - now
    if rate_limit_remaining > 0:
        return ("rate_limited", int(rate_limit_remaining))

    if account.failures > 0:
        backoff_multiplier = min(2 ** (account.failures - 1), ACCOUNT_MAX_BACKOFF_MULTIPLIER)
        effective_timeout = ACCOUNT_RECOVERY_TIMEOUT * backoff_multiplier
        remaining = effective_timeout - (now - account.last_failure_time)
        if remaining > 0:
            return ("cooling_down", int(remaining))

    if account.auth_manager is None:
        return ("uninitialized", 0)

    # Telemetry says the allowance is gone and overage is off, but no 402 window
    # is active: either the quarantine already expired or the account has not
    # been tried since the quota ran out. Either way the account cannot serve,
    # so it is excluded from routing like any other spent account.
    if is_quota_depleted(account):
        remaining = account.quota_resets_at - now
        return ("quota_depleted", int(remaining) if remaining > 0 else 0)

    return ("available", 0)


def is_quota_depleted(account: "Account") -> bool:
    """Report whether usage says this account's allowance is spent.

    Requires all three facts to be conclusive: a reading exists, it shows nothing
    left, and overage billing is off so 100% really is the ceiling. An unpolled
    account, an unreadable reading, or an unknown overage status is never treated
    as depleted - the gateway does not exclude an account on a guess.

    Unlike ``quota_exhausted``, this is derived from telemetry rather than an
    upstream refusal, so it is the one exclusion that can be wrong. Callers that
    route on it must keep the last-resort path in ``get_next_account``: a stalled
    usage poll must not be able to empty the pool.
    """
    return (
        account.quota_headroom is not None and account.quota_headroom <= 0.0 and account.quota_overage_enabled is False
    )


def _reason_of(body: str) -> Optional[str]:
    """Pull the reason code out of an upstream error body, if it carries one.

    The runtime host states the verdict in `reason`; the legacy q.* host sends
    `reason: null` and words it in the message instead. Both callers pass the
    result to is_suspension_error, which handles either shape.
    """
    try:
        parsed = json.loads(body)
    except Exception:
        return None
    reason = parsed.get("reason") if isinstance(parsed, dict) else None
    return str(reason) if reason else None


def _format_duration(seconds: float) -> str:
    """
    Format duration in human-readable format.

    Args:
        seconds: Duration in seconds

    Returns:
        Formatted string (e.g., "30s", "5m", "2h", "1d")

    Examples:
        >>> _format_duration(30)
        '30s'
        >>> _format_duration(300)
        '5m'
        >>> _format_duration(7200)
        '2h'
        >>> _format_duration(86400)
        '1d'
    """
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds / 60)}m"
    elif seconds < 86400:
        return f"{int(seconds / 3600)}h"
    else:
        return f"{int(seconds / 86400)}d"


@dataclass
class AccountStats:
    """
    Statistics for account usage.

    Tracks request counts for monitoring and future web UI.
    """

    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0


@dataclass
class Account:
    """
    Complete account entity with all dependencies.

    Represents a single Kiro account with its authentication,
    model cache, resolver, and runtime state.

    Attributes:
        id: Unique identifier (path to credentials file)
        auth_manager: Authentication manager (lazy initialized)
        model_cache: Model metadata cache (lazy initialized)
        model_resolver: Model resolver (lazy initialized)
        failures: Consecutive failure count (for Circuit Breaker)
        last_failure_time: Timestamp of last failure
        rate_limited_until: Timestamp until which the account is rate limited.
            Set by a 429 and deliberately kept out of the Circuit Breaker: a
            rate rejection means "asked too quickly", not "broken", so the
            account rotates out briefly and returns at full health. Not
            persisted; the window is shorter than a restart.
        quota_exhausted_until: Timestamp until which the account is out of
            monthly quota (402 MONTHLY_REQUEST_COUNT). The account cannot serve
            any request until its quota resets, so it leaves the rotation
            entirely - no probabilistic retry reaches it. Persisted, because
            the state outlives the process.
        suspended_until: Timestamp until which the account is locked upstream
            (403 with a suspension message). Unlike every other exclusion this
            one cannot expire on its own - only Kiro support lifts it - so the
            account leaves the rotation completely and the Circuit Breaker is
            left untouched. Persisted; a restart must not resurrect it.
        auth_dead_until: Timestamp until which the account's stored refresh token
            is known rejected by the auth host (401, or 400 after the raw-source
            reload already failed). Ranked above ``suspended_until`` because the
            account cannot obtain a token at all, so no upstream verdict about it
            is even reachable; only a re-login clears it. Like a suspension it
            leaves the Circuit Breaker untouched - a probabilistic retry would
            only spend a request re-proving the credential is dead. Persisted,
            because the condition outlives the process.
        models_cached_at: Timestamp of last model cache update
        quota_headroom: Fraction of the monthly quota still unused (0.0-1.0), or
            None when no usage reading is available. Fed by control-plane usage
            polling. Weights routing while any quota remains, and at 0.0 combines
            with ``quota_overage_enabled`` to exclude the account outright - see
            ``is_quota_depleted``. That exclusion is inferred rather than observed,
            so ``get_next_account`` keeps a last-resort pass that ignores it; a
            stalled poll must not empty the pool. Not persisted - a stale headroom
            is worse than none, and load re-seeds it from the usage rows.
        quota_resets_at: Epoch seconds at which the monthly allowance next
            resets, from the control-plane usage poll, or 0.0 when unknown. This
            is what a 402 quarantine waits for: a fixed window expires while the
            quota is still spent, re-admitting an account that can only answer
            402 again. Not persisted with runtime state - it is re-seeded from
            the usage rows on load, like ``quota_headroom``.
        quota_overage_enabled: Whether the account may keep serving past its
            allowance, or None when the status is unknown. Load-bearing for
            routing, not display: it separates "at 100% and therefore done" from
            "at 100% but billing overage", and only the former is excluded. None
            never excludes - an unknown status is not evidence.
        stats: Usage statistics
    """

    id: str
    auth_manager: Optional[KiroAuthManager] = None
    model_cache: Optional[ModelInfoCache] = None
    model_resolver: Optional[ModelResolver] = None
    failures: int = 0
    last_failure_time: float = 0.0
    rate_limited_until: float = 0.0
    quota_exhausted_until: float = 0.0
    suspended_until: float = 0.0
    auth_dead_until: float = 0.0
    models_cached_at: float = 0.0
    quota_headroom: Optional[float] = None
    quota_resets_at: float = 0.0
    quota_overage_enabled: Optional[bool] = None
    stats: AccountStats = field(default_factory=AccountStats)


@dataclass
class RateObservation:
    """
    One routing verdict: when, which account, at what rate, and how it ended.

    Persisted, so both the rate chart and the inferred limit survive a restart.
    Storing the rate alongside the outcome is what makes that possible: a request
    count can only be derived from a full event history, which a fresh process
    does not have.

    Attributes:
        at: Unix timestamp of the verdict
        account_id: Internal account ID
        rpm: Requests in the trailing RATE_WINDOW_SECONDS at that instant
        rejected: True when the upstream answered with a rate rejection
    """

    at: float
    account_id: str
    rpm: int
    rejected: bool
    outcome: str = "success"


@dataclass
class ModelAccountList:
    """
    List of accounts for a specific model.

    Attributes:
        accounts: List of account IDs that have this model

    Note: next_index removed - now using global _current_account_index
    """

    accounts: List[str] = field(default_factory=list)


class AccountManager:
    """
    Manages multiple Kiro accounts with intelligent failover.

    Responsibilities:
    - Load account sources from the private SQLite store
    - Lazy initialization of accounts
    - Select next available account (Circuit Breaker + Sticky)
    - Track statistics and failures
    - Persist runtime state to the private SQLite store

    Example:
        >>> manager = AccountManager()
        >>> await manager.load_credentials()
        >>> await manager.load_state()
        >>> account = await manager.get_next_account("claude-opus-4.5")
        >>> await manager.report_success(account.id, "claude-opus-4.5")
    """

    def __init__(self):
        """Initialize AccountManager against the private SQLite account store."""
        self._accounts: Dict[str, Account] = {}
        self._model_to_accounts: Dict[str, ModelAccountList] = {}
        self._lock = asyncio.Lock()
        # Slow credential/model I/O must never hold the routing lock.  Tasks are
        # per account so concurrent selectors share one initialization/refresh.
        self._initializations: Dict[str, asyncio.Task[bool]] = {}
        self._model_refreshes: Dict[str, asyncio.Task[None]] = {}
        self._dirty = False
        self._credentials_config: List[Dict] = []
        self._current_account_index: int = 0  # GLOBAL sticky index for all models
        # Bounded ring of recent routing outcomes for the dashboard rate chart.
        # Memory-only: this is observability, not state the router depends on.
        # Routing history outlives the process; see RateObservation.
        self._rate_observations: List[RateObservation] = []
        self._unsaved_rate_observations: List[RateObservation] = []

    async def load_credentials(self) -> None:
        """Load credentials while serializing durable rollback recovery."""
        async with self._lock:
            await self._load_credentials_unlocked()

    async def _load_credentials_unlocked(self) -> None:
        """
        Load account sources from the private SQLite store.

        The caller must hold ``_lock``. Validates each entry and creates
        Account objects; invalid entries are skipped with warnings and folders
        are scanned for credential files.
        """
        try:
            from kiro.store import load_account_sources

            self._credentials_config = load_account_sources()
        except Exception as e:
            logger.error(f"Failed to load credentials: {e}")
            return

        # Process each credential entry
        for entry in self._credentials_config:
            cred_type = entry.get("type")
            path = entry.get("path")
            enabled = entry.get("enabled", True)

            if not enabled:
                continue

            # Validate required fields based on type
            if not cred_type:
                logger.warning(f"Invalid credential entry (missing type): {entry}")
                continue

            # For json/sqlite types, path is required
            if cred_type in ("json", "sqlite") and not path:
                logger.warning(f"Invalid credential entry (type={cred_type} requires path): {entry}")
                continue

            # For refresh_token type, refresh_token field is required
            if cred_type == "refresh_token" and not entry.get("refresh_token"):
                logger.warning(f"Invalid credential entry (type=refresh_token requires refresh_token field): {entry}")
                continue

            # Handle refresh_token type (no path processing needed)
            if cred_type == "refresh_token":
                # Use deterministic hash for refresh_token (hash() is not deterministic between process restarts)
                token = entry.get("refresh_token", "")
                token_hash = hashlib.sha256(token.encode()).hexdigest()[:16]
                account_id = f"refresh_token_{token_hash}"
                self._accounts[account_id] = Account(id=account_id)
                logger.debug(f"Added account: {account_id}")
                continue  # Skip path processing for refresh_token

            if cred_type == "internal":
                account_id = str(entry.get("id", ""))
                if account_id:
                    self._accounts[account_id] = Account(id=account_id)
                continue

            # Handle folder scanning for json/sqlite types
            assert path is not None
            expanded_path = Path(path).expanduser()
            if expanded_path.is_dir():
                logger.info(f"Scanning folder for credentials: {path}")
                for file_path in expanded_path.iterdir():
                    if not file_path.is_file():
                        continue

                    # Validate file before adding as account
                    account_id = str(file_path.resolve())
                    is_valid = False

                    # Try JSON validation
                    if cred_type == "json":
                        try:
                            with open(file_path, "r", encoding="utf-8") as f:
                                data = json.load(f)
                                # Valid if has refreshToken or clientId
                                if "refreshToken" in data or "clientId" in data:
                                    is_valid = True
                        except Exception as e:
                            logger.warning(f"Invalid JSON credentials file {file_path.name}: {e}")

                    # Try SQLite validation
                    elif cred_type == "sqlite":
                        try:
                            import sqlite3

                            conn = sqlite3.connect(str(file_path))
                            cursor = conn.cursor()
                            # Check if auth_kv table exists
                            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='auth_kv'")
                            if cursor.fetchone():
                                is_valid = True
                            conn.close()
                        except Exception as e:
                            logger.warning(f"Invalid SQLite database file {file_path.name}: {e}")

                    if is_valid:
                        self._accounts[account_id] = Account(id=account_id)
                        logger.debug(f"Added account from folder: {account_id}")
                    else:
                        logger.warning(f"Skipping invalid credentials file: {file_path.name}")
            elif expanded_path.is_file() or cred_type == "refresh_token":
                # Single file or refresh_token type
                if cred_type == "refresh_token":
                    # Use deterministic hash for refresh_token (hash() is not deterministic between process restarts)
                    token = entry.get("refresh_token", "")
                    token_hash = hashlib.sha256(token.encode()).hexdigest()[:16]
                    account_id = f"refresh_token_{token_hash}"
                else:
                    account_id = str(expanded_path.resolve())
                self._accounts[account_id] = Account(id=account_id)
                logger.debug(f"Added account: {account_id}")
            else:
                logger.warning(f"Credential path not found: {path}")

        logger.info(f"Loaded {len(self._accounts)} account(s) from credentials")

    async def load_state(self) -> None:
        """
        Load runtime state from the private SQLite store.

        Restores model_to_accounts mapping and account runtime state.
        Creates empty state if file doesn't exist.
        """
        try:
            from kiro.store import load_runtime_state

            state_data = load_runtime_state()
            if state_data is None:
                logger.debug("No persisted account runtime state")
                # Routing weights live in a different table and are seeded even
                # with no runtime state: a first-ever start still has usage rows
                # once the dashboard has polled, and routing blind is exactly
                # the condition weighted selection exists to avoid.
                self._seed_quota_headroom()
                return
            # Restore global current_account_index
            self._current_account_index = state_data.get("current_account_index", 0)

            # Restore model_to_accounts mapping (without next_index)
            for model, data in state_data.get("model_to_accounts", {}).items():
                self._model_to_accounts[model] = ModelAccountList(accounts=data.get("accounts", []))

            # Restore account runtime state
            for account_id, data in state_data.get("accounts", {}).items():
                if account_id in self._accounts:
                    account = self._accounts[account_id]
                    account.failures = data.get("failures", 0)
                    account.last_failure_time = data.get("last_failure_time", 0.0)
                    account.quota_exhausted_until = data.get("quota_exhausted_until", 0.0)
                    account.suspended_until = data.get("suspended_until", 0.0)
                    account.auth_dead_until = data.get("auth_dead_until", 0.0)
                    account.models_cached_at = data.get("models_cached_at", 0.0)

                    stats_data = data.get("stats", {})
                    account.stats = AccountStats(
                        total_requests=stats_data.get("total_requests", 0),
                        successful_requests=stats_data.get("successful_requests", 0),
                        failed_requests=stats_data.get("failed_requests", 0),
                    )

            self._seed_quota_headroom()

            logger.info(f"Loaded state: {len(self._model_to_accounts)} model mappings, {len(self._accounts)} accounts")

        except Exception as e:
            logger.error(f"Failed to load state: {e}")

    def _seed_quota_headroom(self) -> None:
        """Prime routing weights from the last persisted usage readings.

        Routing weight is deliberately not part of the runtime state document -
        a stale headroom would misroute - but starting with none means every
        cold start and every blue/green handoff routes blind until the first
        usage poll, up to USAGE_REFRESH_INTERVAL_SECONDS later. The persisted
        dashboard readings are the best available bridge, and the next poll
        overwrites them.
        """
        try:
            from kiro.store import load_quota_headroom, load_quota_period

            headroom = load_quota_headroom()
            period = load_quota_period()
        except Exception as e:
            logger.debug(f"No persisted quota headroom to seed routing weights: {e}")
            return

        seeded = 0
        for account_id, value in headroom.items():
            account = self._accounts.get(account_id)
            if account is None:
                continue
            account.quota_headroom = min(1.0, max(0.0, value))
            seeded += 1

        # Reset date and overage flag are seeded independently of headroom: a row
        # can carry a usable reset date while its usage reading is unusable, a
        # restart mid-quarantine needs the date to avoid falling back to the fixed
        # window, and the depleted exclusion needs the overage flag before the
        # first poll or a spent account reads as ready again.
        period_seeded = 0
        for account_id, (reset_at, overage) in period.items():
            account = self._accounts.get(account_id)
            if account is None:
                continue
            account.quota_resets_at = reset_at or 0.0
            account.quota_overage_enabled = overage
            period_seeded += 1

        if seeded or period_seeded:
            logger.info(
                f"Seeded routing quota headroom for {seeded} account(s), quota period for {period_seeded} account(s)"
            )

    async def reload_durable_state(self) -> None:
        """Replace the live pool with the latest durable handoff snapshot."""
        async with self._lock:
            self._accounts.clear()
            self._model_to_accounts.clear()
            self._current_account_index = 0
            await self._load_credentials_unlocked()
            await self.load_state()

    async def flush_for_handoff(self) -> None:
        """Persist a final routing snapshot while account selection is stopped."""
        async with self._lock:
            await self._save_state(raise_errors=True)

    async def _save_state(self, *, raise_errors: bool = False) -> bool:
        """
        Save runtime state transactionally in SQLite.

        Uses tmp file + rename for atomic write. Background callers keep the
        best-effort default; destructive operations can require an observable
        failure with ``raise_errors=True``.
        """
        state_data = self._state_document()

        try:
            from kiro.store import save_runtime_state

            written = save_runtime_state(state_data)
            # Older store implementations returned None after writing. False is
            # the explicit signal that this process does not own runtime writes.
            if written is False:
                message = "Runtime state write skipped: this process is not the active writer"
                logger.debug(message)
                if raise_errors:
                    raise RuntimeError(message)
                return False
            logger.debug("State saved successfully")
            return True

        except Exception as e:
            logger.error(f"Failed to save state: {e}")
            if raise_errors:
                raise
            return False

    def _state_document(self) -> Dict:
        """Return the complete durable runtime state document."""
        return {
            "current_account_index": self._current_account_index,
            "accounts": {
                account_id: {
                    "failures": account.failures,
                    "last_failure_time": account.last_failure_time,
                    "quota_exhausted_until": account.quota_exhausted_until,
                    "suspended_until": account.suspended_until,
                    "auth_dead_until": account.auth_dead_until,
                    "models_cached_at": account.models_cached_at,
                    "stats": {
                        "total_requests": account.stats.total_requests,
                        "successful_requests": account.stats.successful_requests,
                        "failed_requests": account.stats.failed_requests,
                    },
                }
                for account_id, account in self._accounts.items()
            },
            "model_to_accounts": {model: {"accounts": mal.accounts} for model, mal in self._model_to_accounts.items()},
        }

    def _remove_account_state(self, account_id: str) -> None:
        """Remove one account from live routing and all persisted runtime state.

        The caller must hold ``_lock`` so account selection cannot observe a
        partially updated pool.
        """
        account_ids = list(self._accounts)
        removed_index = account_ids.index(account_id) if account_id in self._accounts else None
        current_index = self._current_account_index % len(account_ids) if account_ids else 0

        self._accounts.pop(account_id, None)

        for model in list(self._model_to_accounts):
            model_accounts = self._model_to_accounts[model]
            model_accounts.accounts = [known_id for known_id in model_accounts.accounts if known_id != account_id]
            if not model_accounts.accounts:
                del self._model_to_accounts[model]

        self._rate_observations = [item for item in self._rate_observations if item.account_id != account_id]
        self._unsaved_rate_observations = [
            item for item in self._unsaved_rate_observations if item.account_id != account_id
        ]

        remaining_count = len(self._accounts)
        if not remaining_count:
            self._current_account_index = 0
        elif removed_index is None:
            self._current_account_index = current_index % remaining_count
        elif removed_index < current_index:
            self._current_account_index = current_index - 1
        elif removed_index == current_index:
            self._current_account_index = min(current_index, remaining_count - 1)
        else:
            self._current_account_index = current_index

        self._dirty = True

    async def save_state_periodically(self) -> None:
        """
        Background task for periodic state saving.

        Saves state every STATE_SAVE_INTERVAL_SECONDS if dirty flag is set.
        """
        while True:
            await asyncio.sleep(STATE_SAVE_INTERVAL_SECONDS)

            if self._dirty:
                async with self._lock:
                    if await self._save_state():
                        self._dirty = False

    async def _initialize_account(self, account_id: str) -> bool:
        """
        Initialize account (lazy initialization).

        Creates auth_manager, fetches models, creates cache and resolver.

        Args:
            account_id: Account ID to initialize

        Returns:
            True if successful, False otherwise
        """
        async with self._lock:
            account = self._accounts.get(account_id)
            creds_config = self._credentials_for_account(account_id)
        if not account:
            return False

        try:
            if not creds_config:
                logger.error(f"No credentials config found for account: {account_id}")
                return False

            # Create KiroAuthManager based on type
            cred_type = creds_config.get("type")
            if cred_type == "json":
                auth_manager = KiroAuthManager(
                    creds_file=account_id,
                    profile_arn=creds_config.get("profile_arn"),
                    region=creds_config.get("region", "us-east-1"),
                    api_region=creds_config.get("api_region"),
                )
            elif cred_type == "sqlite":
                auth_manager = KiroAuthManager(
                    sqlite_db=account_id,
                    profile_arn=creds_config.get("profile_arn"),
                    region=creds_config.get("region", "us-east-1"),
                    api_region=creds_config.get("api_region"),
                )
            elif cred_type == "refresh_token":
                auth_manager = KiroAuthManager(
                    refresh_token=creds_config.get("refresh_token"),
                    profile_arn=creds_config.get("profile_arn"),
                    region=creds_config.get("region", "us-east-1"),
                    api_region=creds_config.get("api_region"),
                )
            elif cred_type == "internal":
                auth_manager = KiroAuthManager(
                    internal_account_id=account_id,
                    profile_arn=creds_config.get("profile_arn"),
                    region=creds_config.get("region", "us-east-1"),
                    api_region=creds_config.get("api_region"),
                )
            else:
                logger.error(f"Unknown credential type: {cred_type}")
                return False

            # Get token to verify credentials
            await auth_manager.get_access_token()

            # Determine if we should fetch models or use static list
            if _is_runtime_endpoint(auth_manager):
                # New runtime endpoint does not provide /ListAvailableModels (AWS limitation)
                # Use static list without attempting request
                logger.debug(f"Account {account_id}: Using static model list for runtime.kiro.dev endpoint")
                models_list = FALLBACK_MODELS
            else:
                # Old endpoint - attempt to fetch dynamic model list
                # Fetch models list with retry + fallback
                params = {"origin": "AI_EDITOR"}
                if auth_manager.auth_type == AuthType.KIRO_DESKTOP and auth_manager.profile_arn:
                    params["profileArn"] = auth_manager.profile_arn

                list_models_url = f"{auth_manager.q_host}/ListAvailableModels"

                # Use KiroHttpClient for retry logic (3 attempts with exponential backoff)
                http_client = KiroHttpClient(auth_manager, shared_client=None)

                try:
                    response = await http_client.request_with_retry(
                        method="GET", url=list_models_url, json_data=None, params=params, stream=False
                    )

                    if response.status_code == 200:
                        data = response.json()
                        models_list = data.get("models", [])
                    elif self._quarantine_if_suspended(account_id, account, response):
                        # A locked account must not fall back to the static list:
                        # that is what left it advertising every model and drawing
                        # traffic it can only answer 403 to.
                        models_list = []
                    else:
                        # Shouldn't happen (retry handles non-200), but keep for safety
                        raise Exception(f"HTTP {response.status_code}")

                except Exception as e:
                    # All retries exhausted - use fallback
                    logger.error(f"Failed to fetch models for {account_id} after retries: {e}")
                    logger.warning(
                        "Using pre-configured fallback models. Models will be refreshed on next TTL cycle when network recovers."
                    )
                    models_list = FALLBACK_MODELS

                finally:
                    await http_client.close()

            # Create model cache and update
            model_cache = ModelInfoCache()
            await model_cache.update(models_list)

            # Add hidden models
            for display_name, internal_id in HIDDEN_MODELS.items():
                model_cache.add_hidden_model(display_name, internal_id)

            # Create model resolver
            model_resolver = ModelResolver(
                cache=model_cache, hidden_models=HIDDEN_MODELS, aliases=MODEL_ALIASES, hidden_from_list=HIDDEN_FROM_LIST
            )

            available_models = model_resolver.get_available_models()
            async with self._lock:
                # Deletion or source replacement may happen while token/model I/O
                # is in flight. Never resurrect or initialize the wrong source.
                if (
                    self._accounts.get(account_id) is not account
                    or self._credentials_for_account(account_id) is not creds_config
                ):
                    return False
                account.auth_manager = auth_manager
                account.model_cache = model_cache
                account.model_resolver = model_resolver
                account.models_cached_at = time.time()
                for model in available_models:
                    if model not in self._model_to_accounts:
                        self._model_to_accounts[model] = ModelAccountList()
                    if account_id not in self._model_to_accounts[model].accounts:
                        self._model_to_accounts[model].accounts.append(account_id)
                self._dirty = True

            logger.info(f"Initialized account: {account_id} ({len(available_models)} models)")
            return True

        except CredentialDeadError as e:
            # Initialization is where a dead credential usually surfaces first,
            # since obtaining a token is the first thing it does. Park the account
            # here rather than only counting a failure: otherwise the caller adds
            # a Circuit Breaker cooldown that expires and re-admits an account
            # whose every future request must fail at the same token step.
            await self.report_credential_dead(account_id, e.status_code)
            return False
        except Exception as e:
            logger.error(f"Failed to initialize account {account_id}: {e}")
            return False

    def _credentials_for_account(self, account_id: str) -> Optional[Dict]:
        """Find the source that explicitly owns ``account_id``.

        Only file-backed source types participate in path matching; this avoids
        an internal source with no path resolving to the current directory and
        accidentally claiming unrelated accounts.
        """
        for entry in self._credentials_config:
            cred_type = entry.get("type")
            if cred_type == "internal":
                if str(entry.get("id", "")) == account_id:
                    return entry
                continue
            if cred_type == "refresh_token":
                token = entry.get("refresh_token")
                if not isinstance(token, str) or not token:
                    continue
                token_hash = hashlib.sha256(token.encode()).hexdigest()[:16]
                if account_id == f"refresh_token_{token_hash}":
                    return entry
                continue
            if cred_type not in ("json", "sqlite") or not entry.get("path"):
                continue
            expanded_path = Path(str(entry["path"])).expanduser()
            resolved = str(expanded_path.resolve())
            if resolved == account_id or (expanded_path.is_dir() and account_id.startswith(resolved + os.sep)):
                return entry
        return None

    async def initialize_account(self, account_id: str) -> bool:
        """Initialize an account outside the global lock with per-account single-flight."""
        async with self._lock:
            account = self._accounts.get(account_id)
            if account is None:
                return False
            if account.auth_manager is not None:
                return True
            task = self._initializations.get(account_id)
            if task is None:
                task = asyncio.create_task(self._initialize_account(account_id))
                self._initializations[account_id] = task
        try:
            return await asyncio.shield(task)
        finally:
            async with self._lock:
                if self._initializations.get(account_id) is task and task.done():
                    self._initializations.pop(account_id, None)

    async def _refresh_account_models_singleflight(self, account_id: str) -> None:
        """Refresh one account outside the routing lock, sharing concurrent work."""
        async with self._lock:
            task = self._model_refreshes.get(account_id)
            if task is None:
                task = asyncio.create_task(self._refresh_account_models(account_id))
                self._model_refreshes[account_id] = task
        try:
            await asyncio.shield(task)
        finally:
            async with self._lock:
                if self._model_refreshes.get(account_id) is task and task.done():
                    self._model_refreshes.pop(account_id, None)

    def _quarantine_if_suspended(self, account_id: str, account: "Account", response: httpx.Response) -> bool:
        """Park an account whose model listing came back as an upstream lock.

        ListAvailableModels is the first upstream call an account makes, so a
        suspension can be caught here before a client request is spent on it.
        Returns whether the account was quarantined.
        """
        body = response.text
        if not is_suspension_error(response.status_code, body, _reason_of(body)):
            return False
        account.suspended_until = time.time() + ACCOUNT_SUSPENSION_QUARANTINE
        self._dirty = True
        logger.error(
            f"Account {account_id} is SUSPENDED upstream (detected while listing models); "
            f"excluded from routing for {_format_duration(ACCOUNT_SUSPENSION_QUARANTINE)}. "
            f"Kiro support must restore this account."
        )
        return True

    async def report_credential_dead(self, account_id: str, status_code: int) -> None:
        """Park an account whose refresh token the auth host has rejected.

        Kept separate from ``report_failure`` because this verdict arrives before
        any model is chosen: the account never reached the data plane, so there is
        no reason code or upstream message to classify. Like a suspension it
        leaves the Circuit Breaker alone - inflating ``failures`` would only add
        an unrelated backoff to an account that is already fully excluded, and
        the 10% probabilistic retry would spend real requests re-proving a dead
        credential.
        """
        async with self._lock:
            account = self._accounts.get(account_id)
            if not account:
                return
            already_parked = account.auth_dead_until > time.time()
            account.auth_dead_until = time.time() + ACCOUNT_AUTH_DEAD_QUARANTINE
            account.stats.total_requests += 1
            account.stats.failed_requests += 1
            self._dirty = True
            if not already_parked:
                self._record_routing_event(account_id, "auth_dead")
                logger.error(
                    f"Account {account_id} credential is DEAD (HTTP {status_code} from the auth host); "
                    f"excluded from routing for {_format_duration(ACCOUNT_AUTH_DEAD_QUARANTINE)}. "
                    f"Re-register or re-login this account to restore it."
                )

    async def _refresh_account_models(self, account_id: str) -> None:
        """
        Refresh model cache for account (TTL refresh).

        Args:
            account_id: Account ID to refresh
        """
        async with self._lock:
            account = self._accounts.get(account_id)
            if not account or not account.auth_manager:
                return
            auth_manager = account.auth_manager
            model_cache = account.model_cache
            model_resolver = account.model_resolver
        assert model_cache is not None
        assert model_resolver is not None

        # Check if using runtime endpoint (no dynamic model list available)
        if _is_runtime_endpoint(auth_manager):
            # Runtime endpoint does not provide /ListAvailableModels
            # Use static list and update cache timestamp
            logger.debug(
                f"Account {account_id}: Skipping model refresh for runtime.kiro.dev endpoint (using static list)"
            )
            async with self._lock:
                if self._accounts.get(account_id) is not account or account.auth_manager is not auth_manager:
                    return
                await model_cache.update(FALLBACK_MODELS)
                account.models_cached_at = time.time()
                self._dirty = True
            return

        # Old endpoint - attempt to fetch dynamic model list
        # Use KiroHttpClient for retry logic
        http_client = KiroHttpClient(auth_manager, shared_client=None)

        try:
            params = {"origin": "AI_EDITOR"}
            if auth_manager.auth_type == AuthType.KIRO_DESKTOP and auth_manager.profile_arn:
                params["profileArn"] = auth_manager.profile_arn

            list_models_url = f"{auth_manager.q_host}/ListAvailableModels"

            response = await http_client.request_with_retry(
                method="GET", url=list_models_url, json_data=None, params=params, stream=False
            )

            if response.status_code != 200:
                # A TTL refresh is also an upstream verdict. Without this check a
                # suspension arriving here was swallowed and the stale cache kept
                # the locked account advertising models indefinitely.
                async with self._lock:
                    if self._accounts.get(account_id) is account and account.auth_manager is auth_manager:
                        self._quarantine_if_suspended(account_id, account, response)
            else:
                data = response.json()
                models_list = data.get("models", [])
                async with self._lock:
                    if self._accounts.get(account_id) is not account or account.auth_manager is not auth_manager:
                        return
                    await model_cache.update(models_list)
                    account.models_cached_at = time.time()
                    for model in model_resolver.get_available_models():
                        if model not in self._model_to_accounts:
                            self._model_to_accounts[model] = ModelAccountList()
                        if account_id not in self._model_to_accounts[model].accounts:
                            self._model_to_accounts[model].accounts.append(account_id)
                    self._dirty = True
                    logger.debug(f"Refreshed models for {account_id}")

        except Exception as e:
            # All retries exhausted - keep using stale cache
            logger.warning(f"Failed to refresh models for {account_id} after retries: {e}")

        finally:
            await http_client.close()

    def _quota_quarantine_until(self, account: Account, now: Optional[float] = None) -> float:
        """Return when a 402-exhausted account should re-enter the rotation.

        The quarantine exists to wait out a spent monthly allowance, so it ends
        when that allowance resets. A fixed interval was measured expiring ~34-40
        hours before the reset on live accounts, which put three accounts back in
        the pool reading "ready" while still at 1000/1000 - they could only answer
        402 again.

        ``ACCOUNT_QUOTA_QUARANTINE`` remains the fallback for when the reset date
        is unknown, and also the floor and ceiling: a reset that is already past
        or implausibly far out (a bad or stale reading) must not translate into
        an instant retry or an unbounded exclusion.
        """
        now = time.time() if now is None else now
        floor = now + ACCOUNT_QUOTA_QUARANTINE
        reset_at = account.quota_resets_at
        if reset_at <= 0:
            return floor
        # A small margin past the reset: the upstream boundary is a date, and
        # retrying a second early just spends another request on a 402.
        target = reset_at + ACCOUNT_QUOTA_RESET_MARGIN
        return max(floor, min(target, now + ACCOUNT_QUOTA_QUARANTINE_MAX))

    def set_quota_headroom(self, account_id: str, headroom: Optional[float]) -> None:
        """Record how much monthly quota an account has left.

        Called by the control-plane usage refresh. Kept lock-free and
        non-throwing: a missing value degrades to the unknown-quota weight, which
        never excludes, so a failed poll cannot take an account out of the pool.
        A reading of 0.0 does gate routing once overage is known to be off (see
        ``is_quota_depleted``), which is why an unusable reading must arrive here
        as None rather than as a zero.

        Args:
            account_id: Internal account ID
            headroom: Unused fraction of the monthly quota (0.0-1.0), or None
                when the reading is unavailable. Values outside the range are
                clamped.
        """
        account = self._accounts.get(account_id)
        if account is None:
            return
        if headroom is None:
            account.quota_headroom = None
            return
        account.quota_headroom = min(1.0, max(0.0, float(headroom)))

    def set_quota_period(self, account_id: str, resets_at: Optional[float], overage_enabled: Optional[bool]) -> None:
        """Record when the quota resets and whether overage keeps it serving.

        Same contract as ``set_quota_headroom``: lock-free and non-throwing. The
        reset time bounds a 402 quarantine so it ends with the allowance instead of
        on a fixed timer. The overage flag separates "spent and therefore done"
        from "spent but still billing", and only the first of those is excluded
        from routing, so passing None (unknown) must leave the account eligible.

        Args:
            account_id: Internal account ID
            resets_at: Epoch seconds of the next quota reset, or None/non-positive
                when unknown
            overage_enabled: Whether the account may serve past its allowance, or
                None when the reading does not say
        """
        account = self._accounts.get(account_id)
        if account is None:
            return
        try:
            candidate = float(resets_at) if resets_at else 0.0
        except (TypeError, ValueError):
            candidate = 0.0
        # Reject inf/nan here too, not just at the parse sites: this field is
        # serialized straight onto the accounts route, where a non-finite value
        # fails JSON encoding for every account at once.
        account.quota_resets_at = candidate if math.isfinite(candidate) and candidate > 0 else 0.0
        account.quota_overage_enabled = overage_enabled

    def _routing_weight(self, account: Account) -> float:
        """Return the selection weight for one account.

        The weight is the account's remaining quota *fraction*, not its absolute
        remaining requests. Absolute headroom would hand a 1000-request account
        20x the traffic of a 50-request one and concentrate it hard enough to
        trip the per-account request-rate limit; the fraction drains every
        account toward empty at the same relative pace, which also spreads the
        rate-limit pressure.

        The result is always positive. A zero weight cannot be sampled, so
        several zero-weight accounts would fall back to a fixed order and the
        ones behind the first would never be reached - the starvation this
        policy exists to remove. Operators can drive a category's share
        arbitrarily low, but not to zero; removing an account from rotation is
        the health policy's job, not a weight's.
        """
        headroom = account.quota_headroom
        if headroom is None:
            return max(ACCOUNT_UNKNOWN_QUOTA_WEIGHT, MINIMUM_ROUTING_WEIGHT)
        # A spent allowance is normally excluded outright, so this weight only
        # decides ordering among spent accounts on the last-resort pass, or for an
        # account at 0% whose overage status leaves it eligible.
        if headroom <= 0.0:
            return max(ACCOUNT_DEPLETED_QUOTA_WEIGHT, MINIMUM_ROUTING_WEIGHT)
        return max(headroom, MINIMUM_ROUTING_WEIGHT)

    def _weighted_candidate_order(self, account_ids: List[str]) -> List[str]:
        """Order candidates by a quota-weighted random draw, best first.

        Every account keeps a nonzero chance of being picked, so this is a
        preference, not a filter: the caller still walks the whole list and
        applies health policy per candidate. Selection uses weighted sampling
        without replacement (exponential jump keys), which needs no persisted
        cursor - the reason the sticky index could starve an account in the
        first place.
        """
        if not ACCOUNT_QUOTA_WEIGHTED_ROUTING:
            start = self._current_account_index
            return [account_ids[(start + offset) % len(account_ids)] for offset in range(len(account_ids))]

        keyed: List[Tuple[float, str]] = []
        for account_id in account_ids:
            account = self._accounts.get(account_id)
            # _routing_weight is always positive, and a vanished account still
            # gets a real draw rather than a fixed position: a constant key would
            # tie with every other such account and order them by insertion,
            # which is how the sticky cursor starved everything behind it.
            weight = self._routing_weight(account) if account is not None else MINIMUM_ROUTING_WEIGHT
            keyed.append((random.expovariate(1.0) / weight, account_id))

        keyed.sort(key=lambda item: item[0])
        return [account_id for _, account_id in keyed]

    async def get_next_account(self, model: str, exclude_accounts: Optional[set] = None) -> Optional[Account]:
        """
        Get next available account for model (Circuit Breaker + quota weighting).

        Implements:
        - Quota-weighted selection order (accounts with more remaining quota are
          preferred; every account keeps a nonzero chance)
        - Circuit Breaker with exponential backoff
        - Probabilistic retry for "dead" accounts (10%)
        - Short skip for rate-limited accounts (no failure penalty)
        - Full exclusion of quota-exhausted accounts (no probabilistic retry)
        - Exclusion of accounts whose usage reports the allowance spent, lifted
          on a last-resort pass so stale telemetry cannot empty the pool
        - TTL-based model cache refresh
        - Exclusion of already-tried accounts in current failover loop

        Args:
            model: Model name (will be normalized)
            exclude_accounts: Set of account IDs to exclude (already tried in current failover loop)

        Returns:
            Account object or None if no accounts available
        """
        # Two passes. The first honors every exclusion. If that finds nothing, the
        # second retries ignoring the one exclusion that is inferred rather than
        # observed: a usage reading can be stale or its poll stalled, and answering
        # 402 beats answering "no accounts available" when the alternative is a
        # pool that is only empty according to telemetry.
        account = await self._select_account(model, exclude_accounts)
        if account is not None:
            return account

        # Only worth a second pass if that exclusion actually applied to someone;
        # otherwise the first pass already considered every account.
        async with self._lock:
            depleted = [a.id for a in self._accounts.values() if is_quota_depleted(a)]
        if not depleted:
            return None

        account = await self._select_account(model, exclude_accounts, last_resort=True)
        if account is not None:
            logger.warning(
                f"Routing to {account.id} despite usage reporting its quota spent: no other account is eligible"
            )
        return account

    async def _select_account(
        self, model: str, exclude_accounts: Optional[set] = None, last_resort: bool = False
    ) -> Optional[Account]:
        """One selection pass. See ``get_next_account`` for the policy.

        ``last_resort`` lifts only the telemetry-derived quota exclusion; every
        exclusion backed by an upstream response still applies.
        """
        async with self._lock:
            all_account_ids = list(self._accounts)
            single_account = len(all_account_ids) == 1
            candidate_order = self._weighted_candidate_order(all_account_ids) if all_account_ids else []

        for account_id in candidate_order:
            async with self._lock:
                account = self._accounts.get(account_id)
                if account is None:
                    continue

                # Skip accounts already tried in current failover loop
                if exclude_accounts and account_id in exclude_accounts:
                    continue

                # A sole account bypasses health policy so callers see the real
                # upstream error rather than a generic unavailable response.
                if not single_account:
                    # A rejected refresh token cannot be renewed by retrying. The
                    # account is excluded outright, ahead of every other check:
                    # without a token it cannot reach the upstream at all, so
                    # there is no verdict left to discover and each attempt only
                    # spends a failover hop re-proving the credential is dead.
                    if account.auth_dead_until > time.time():
                        continue

                    # An upstream suspension takes the account out of the rotation
                    # completely. No probabilistic retry: the lock is lifted by Kiro
                    # support, never by another request, and each attempt still costs
                    # a full retry storm plus a user-visible failure.
                    if account.suspended_until > time.time():
                        continue

                    # Skip accounts that cannot serve any request. A quota-exhausted
                    # account is out of the rotation entirely until its quarantine
                    # expires: no probabilistic retry, because there is nothing to
                    # discover before the quota resets.
                    if account.quota_exhausted_until > time.time():
                        continue

                    # Skip accounts whose usage says the allowance is spent. Unlike
                    # the checks above this is derived from telemetry rather than an
                    # upstream refusal, so it is skipped on the last-resort pass:
                    # a stalled usage poll must not be able to empty the pool.
                    if not last_resort and is_quota_depleted(account):
                        continue

                    # Skip accounts inside their rate-limit window. This is checked
                    # before the Circuit Breaker and has no probabilistic retry:
                    # retrying a rate-limited account only earns another 429.
                    if account.rate_limited_until > time.time():
                        continue

                    # Check Circuit Breaker (Half-Open state with exponential backoff)
                    if account.failures > 0:
                        time_since_failure = time.time() - account.last_failure_time

                        # Exponential backoff: base * 2^(failures - 1), capped at MAX_MULTIPLIER
                        # 1 failure: 60s, 2: 120s, 3: 240s, ..., 12+: 86400s (1 day cap)
                        backoff_multiplier = min(2 ** (account.failures - 1), ACCOUNT_MAX_BACKOFF_MULTIPLIER)
                        effective_timeout = ACCOUNT_RECOVERY_TIMEOUT * backoff_multiplier

                        if time_since_failure < effective_timeout:
                            # Probabilistic retry (10% chance)
                            if random.random() > ACCOUNT_PROBABILISTIC_RETRY_CHANCE:
                                continue
                            logger.info(f"Probabilistic retry for broken account {account_id}")
                        else:
                            # Half-Open: recovery timeout passed
                            logger.info(
                                f"Half-Open state for {account_id} (recovery timeout passed, effective={effective_timeout}s)"
                            )

                needs_initialization = account.auth_manager is None
                needs_refresh = (
                    not needs_initialization
                    and account.models_cached_at > 0
                    and time.time() - account.models_cached_at > ACCOUNT_CACHE_TTL
                )

            if needs_initialization:
                if not await self.initialize_account(account_id):
                    async with self._lock:
                        current = self._accounts.get(account_id)
                        if current is account:
                            current.failures += 1
                            self._dirty = True
                    continue

            if needs_refresh:
                try:
                    await self._refresh_account_models_singleflight(account_id)
                except Exception as e:
                    logger.warning(f"Failed to refresh models for {account_id}: {e}")

            # Any await above allowed deletion or replacement. Revalidate the
            # exact object before exposing it to a route.
            async with self._lock:
                current = self._accounts.get(account_id)
                if current is not account or current.auth_manager is None:
                    continue
                return current

        return None

    async def report_success(self, account_id: str, model: str) -> None:
        """
        Report successful request (reset failures, update stats, sticky, dynamic learning).

        Args:
            account_id: Account ID
            model: Model name
        """
        async with self._lock:
            account = self._accounts.get(account_id)
            if not account:
                return

            # Reset failures
            if account.failures > 0:
                account.failures = 0
                self._dirty = True

            # A success proves the account is accepting requests again, so drop
            # any leftover rate-limit or quota window instead of waiting it out.
            account.rate_limited_until = 0.0
            # A served request proves the credential was renewed - by a
            # re-registration, or by another process writing a fresh token - so a
            # stale death verdict must not outlive the evidence against it.
            if account.auth_dead_until:
                account.auth_dead_until = 0.0
                self._dirty = True
            if account.suspended_until:
                account.suspended_until = 0.0
                logger.info(f"Account {account_id} is serving again; suspension lifted")
            if account.quota_exhausted_until:
                account.quota_exhausted_until = 0.0
                logger.info(f"Account {account_id} is serving again; quota quarantine cleared")

            # Update stats
            account.stats.total_requests += 1
            account.stats.successful_requests += 1
            self._record_routing_event(account_id, "success")
            self._dirty = True

            # Dynamic learning: add model to mapping if successful
            # This allows system to learn about new models not in FALLBACK_MODELS
            normalized_model = normalize_model_name(model)
            if normalized_model not in self._model_to_accounts:
                self._model_to_accounts[normalized_model] = ModelAccountList()
                logger.debug(f"Dynamic learning: discovered new model '{normalized_model}'")
            if account_id not in self._model_to_accounts[normalized_model].accounts:
                self._model_to_accounts[normalized_model].accounts.append(account_id)
                logger.debug(f"Dynamic learning: model '{normalized_model}' works on account {account_id}")
                self._dirty = True

            # Track the last successful account. Under quota-weighted routing
            # this is no longer the selection cursor - it is kept because it is
            # the rotation start for the legacy sticky policy
            # (ACCOUNT_QUOTA_WEIGHTED_ROUTING=false) and part of the persisted
            # state document, which a mixed-version blue/green pair still reads.
            all_account_ids = list(self._accounts.keys())
            try:
                successful_index = all_account_ids.index(account_id)
                if self._current_account_index != successful_index:
                    self._current_account_index = successful_index
                    self._dirty = True
            except ValueError:
                pass

    async def report_failure(
        self,
        account_id: str,
        model: str,
        error_type: ErrorType,
        status_code: int,
        reason: Optional[str],
        message: Optional[str] = None,
    ) -> None:
        """
        Report failed request (update failures, stats, failover).

        Args:
            account_id: Account ID
            model: Model name
            error_type: Error classification (FATAL or RECOVERABLE)
            status_code: HTTP status code
            reason: Error reason from Kiro API
            message: Original upstream message. Required to recognize a
                suspension on the legacy q.* host, which sends reason=null and
                states the verdict only in the message.
        """
        async with self._lock:
            account = self._accounts.get(account_id)
            if not account:
                return

            # Special case: INVALID_MODEL_ID is discovery process, not account failure
            # Account is healthy, model is just not available on this account
            # Log for user visibility but don't penalize account statistics
            if reason == "INVALID_MODEL_ID":
                account.stats.total_requests += 1
                self._dirty = True
                logger.warning(
                    f"Model '{model}' not available on account {account_id}: status={status_code}, reason={reason}"
                )
                return

            # Special case: a request-rate rejection means the account was asked
            # too quickly, not that it is broken. Park it for a few seconds
            # without touching the Circuit Breaker so a momentary burst cannot
            # escalate into an hour-long exclusion and shrink the usable pool.
            if reason == "USER_REQUEST_RATE_EXCEEDED":
                account.rate_limited_until = time.time() + ACCOUNT_RATE_LIMIT_COOLDOWN
                account.stats.total_requests += 1
                account.stats.failed_requests += 1
                self._record_routing_event(account_id, "rate_limited")
                self._dirty = True
                logger.warning(
                    f"Account {account_id} rate limited: "
                    f"status={status_code}, reason={reason}, "
                    f"cooldown={_format_duration(ACCOUNT_RATE_LIMIT_COOLDOWN)} "
                    f"(failures unchanged at {account.failures})"
                )
                return

            # Special case: the account itself is locked upstream. No number of
            # retries or token refreshes can change that, so park it for a long
            # quarantine and leave the Circuit Breaker alone - inflating failures
            # would only add an unrelated backoff to an account already fully
            # excluded.
            #
            # The reason field alone is not the test: the runtime host sends
            # reason=TEMPORARILY_SUSPENDED, but the legacy q.* host sends
            # reason=null and states the verdict only in the message. Checking
            # reason only left Builder ID accounts in the rotation, answering 403
            # to every request forever.
            if is_suspension_error(status_code, message, reason):
                account.suspended_until = time.time() + ACCOUNT_SUSPENSION_QUARANTINE
                account.stats.total_requests += 1
                account.stats.failed_requests += 1
                self._record_routing_event(account_id, "suspended")
                self._dirty = True
                logger.error(
                    f"Account {account_id} is SUSPENDED upstream: "
                    f"status={status_code}, reason={reason}, "
                    f"excluded from routing for {_format_duration(ACCOUNT_SUSPENSION_QUARANTINE)}. "
                    f"Kiro support must restore this account."
                )
                return

            # Special case: the monthly quota is gone, so this account cannot
            # serve anything until it resets. Take it out of the rotation for a
            # long quarantine rather than leaving the Circuit Breaker to leak
            # probabilistic retries into an account that can only answer 402.
            if reason == "MONTHLY_REQUEST_COUNT":
                account.quota_exhausted_until = self._quota_quarantine_until(account)
                account.stats.total_requests += 1
                account.stats.failed_requests += 1
                self._record_routing_event(account_id, "quota_exhausted")
                self._dirty = True
                logger.warning(
                    f"Account {account_id} monthly quota exhausted: "
                    f"status={status_code}, reason={reason}, "
                    f"excluded from routing for "
                    f"{_format_duration(max(0.0, account.quota_exhausted_until - time.time()))}"
                )
                return

            # Update failure count (only for RECOVERABLE)
            if error_type == ErrorType.RECOVERABLE:
                account.failures += 1
                account.last_failure_time = time.time()
                self._dirty = True

                # Calculate backoff for logging
                backoff_multiplier = min(2 ** (account.failures - 1), ACCOUNT_MAX_BACKOFF_MULTIPLIER)
                effective_timeout = ACCOUNT_RECOVERY_TIMEOUT * backoff_multiplier
                logger.warning(
                    f"Account {account_id} failure #{account.failures}: "
                    f"status={status_code}, reason={reason}, "
                    f"cooldown={_format_duration(effective_timeout)}"
                )

            # Update stats
            account.stats.total_requests += 1
            account.stats.failed_requests += 1
            self._record_routing_event(account_id, "failure")
            self._dirty = True

            # GLOBAL STICKY: Do NOT change _current_account_index on failure
            # It only changes on success (GLOBAL sticky behavior)
            # Failover happens through exclude_accounts in get_next_account()

    def _record_routing_event(self, account_id: str, outcome: str) -> None:
        at = time.time()

        # Rate at this instant, counted from persisted observations so the figure
        # is correct on the first request after a restart instead of reporting 1.
        cutoff = at - RATE_WINDOW_SECONDS
        rpm = (
            sum(
                1
                for observation in reversed(self._rate_observations)
                if observation.at > cutoff and observation.account_id == account_id
            )
            + 1
        )

        observation = RateObservation(
            at=at,
            account_id=account_id,
            rpm=rpm,
            rejected=outcome == "rate_limited",
            outcome=outcome,
        )
        self._rate_observations.append(observation)
        self._unsaved_rate_observations.append(observation)

    def drain_unsaved_rate_observations(self) -> List[Tuple[str, float, int, int, str]]:
        """Hand over rate observations not yet written to the dashboard store."""
        pending = self._unsaved_rate_observations
        self._unsaved_rate_observations = []
        return [(item.account_id, item.at, item.rpm, int(item.rejected), item.outcome) for item in pending]

    def restore_unsaved_rate_observations(self, rows: List[Tuple[str, float, int, int, str]]) -> None:
        """Restore a failed persistence batch ahead of newer observations."""
        restored = [
            RateObservation(account_id=account_id, at=at, rpm=rpm, rejected=bool(rejected), outcome=outcome)
            for account_id, at, rpm, rejected, outcome in rows
        ]
        self._unsaved_rate_observations = restored + self._unsaved_rate_observations

    def load_rate_observations(self, rows: List[Tuple[str, float, int, int, str]]) -> None:
        """Restore persisted rate observations after a restart."""
        restored = [
            RateObservation(account_id=account_id, at=at, rpm=rpm, rejected=bool(rejected), outcome=outcome)
            for account_id, at, rpm, rejected, outcome in rows
        ]
        self._rate_observations = restored + self._rate_observations

    def _prune_rate_observations(self, now: float) -> None:
        cutoff = now - RATE_ESTIMATE_WINDOW_SECONDS
        if self._rate_observations and self._rate_observations[0].at < cutoff:
            self._rate_observations = [item for item in self._rate_observations if item.at >= cutoff]

    def estimate_rate_limit(self, account_id: str, now: Optional[float] = None) -> Dict[str, object]:
        """
        Infer the upstream rate limit for one account from observed verdicts.

        A rejection means the rate exceeded the limit and a success means it did
        not, so the samples bracket the limit rather than scattering around it.
        Averaging them would settle above the true limit and report headroom that
        does not exist, so the bound is the lowest rejected rate instead.

        A rejection only counts when it happened at or above the highest rate the
        account served cleanly. Below that it contradicts itself and was caused by
        something else, so it is counted but excluded.

        Only samples inside RATE_ESTIMATE_WINDOW_SECONDS are used. The bound can
        never rise on its own, so without ageing an upstream limit that was
        raised would stay pinned to the old value indefinitely.

        Returns:
            Dict with the inferred limit, the safe rate below it, the width of
            the remaining interval, sample counts, and why a limit is absent.
        """
        now = time.time() if now is None else now
        cutoff = now - RATE_ESTIMATE_WINDOW_SECONDS
        samples = [item for item in self._rate_observations if item.account_id == account_id and item.at >= cutoff]

        served_peak = max((item.rpm for item in samples if not item.rejected), default=0)
        rejections = [item.rpm for item in samples if item.rejected]
        informative = [rpm for rpm in rejections if rpm >= served_peak]
        limit_rpm = min(informative) if informative else None

        if limit_rpm is not None:
            reason = None
        elif rejections:
            reason = "rejections seen only below the rate this account serves cleanly"
        else:
            reason = "no rate rejection observed yet"

        return {
            "limitRpm": limit_rpm,
            "limitUnknownReason": reason,
            "safeRpm": served_peak,
            # How much room is left between proven-safe and known-rejected.
            "limitPrecisionRpm": None if limit_rpm is None else max(0, limit_rpm - served_peak),
            "rateLimitSamples": len(rejections),
            "informativeSamples": len(informative),
            "estimateWindowSeconds": RATE_ESTIMATE_WINDOW_SECONDS,
        }

    def _observations_by_account(self) -> Dict[str, List[RateObservation]]:
        grouped: Dict[str, List[RateObservation]] = {account_id: [] for account_id in self._accounts}
        for observation in self._rate_observations:
            bucket = grouped.get(observation.account_id)
            if bucket is not None:
                bucket.append(observation)
        return grouped

    def request_rate_series(self, window_seconds: int, bucket_seconds: int) -> Dict[str, object]:
        """
        Report per-account request rate, peak load, and the observed rate ceiling.

        Answers what the request log cannot: how hard each account was driven and
        where its 429s landed. The log records the client-facing result, so a 429
        that failover recovered from is filed as a 200 and carries no account.

        Bucket totals alone hide the thing that actually trips a rate limit. A
        bucket holding 30 requests looks like a steady rate, but the upstream
        rejects on instantaneous speed, so 30 requests packed into two seconds
        and 30 spread evenly are different events with the same total. Each
        bucket therefore also reports the highest RPM observed inside it,
        measured as a sliding RATE_WINDOW_SECONDS count at each request instant.

        Kiro publishes no rate limit, so the guide line is inferred from observed
        verdicts by estimate_rate_limit(), which documents the reasoning and the
        ageing window that lets a raised limit recover.

        Args:
            window_seconds: How far back to report
            bucket_seconds: Width of one bucket

        Returns:
            Dict with the bucket width, bucket start timestamps, the RPM
            averaging window, and one entry per account holding its per-bucket
            counts, per-bucket peak RPM, and its rate-limit estimate.
        """
        now = time.time()
        self._prune_rate_observations(now)
        latest_start = int(now // bucket_seconds) * bucket_seconds
        bucket_count = max(1, window_seconds // bucket_seconds)
        starts = [latest_start - offset * bucket_seconds for offset in range(bucket_count - 1, -1, -1)]
        index_of = {start: position for position, start in enumerate(starts)}

        accounts: List[Dict[str, object]] = []

        for account_id, observations in self._observations_by_account().items():
            success = [0] * bucket_count
            rate_limited = [0] * bucket_count
            failure = [0] * bucket_count
            peak_rpm = [0] * bucket_count

            for observation in observations:
                bucket = index_of.get(int(observation.at // bucket_seconds) * bucket_seconds)
                if bucket is None:
                    continue

                if observation.outcome == "success":
                    success[bucket] += 1
                elif observation.outcome == "rate_limited":
                    rate_limited[bucket] += 1
                else:
                    failure[bucket] += 1
                peak_rpm[bucket] = max(peak_rpm[bucket], observation.rpm)

            account = self._accounts.get(account_id)
            accounts.append(
                {
                    "account": account_label(account_id),
                    "success": success,
                    "rateLimited": rate_limited,
                    "failure": failure,
                    "peakRpm": peak_rpm,
                    # The routing state travels with the series so the dashboard
                    # can tell an account that served nothing from one that cannot
                    # serve at all, without joining a second endpoint that reports
                    # the pool as of now against a windowed history.
                    #
                    # Series are seeded from the current pool
                    # (_observations_by_account), so `account` is normally present;
                    # None is kept as an explicit "unknown" rather than defaulting
                    # to a state that would claim something untrue.
                    "routingState": account_routing_state(account, now)[0] if account is not None else None,
                    **self.estimate_rate_limit(account_id, now),
                }
            )

        return {
            "bucketSeconds": bucket_seconds,
            "bucketStarts": starts,
            "rateWindowSeconds": RATE_WINDOW_SECONDS,
            "accounts": accounts,
        }

    def describe_pool_state(self, exclude_accounts: Optional[set] = None) -> str:
        """
        Describe why each account is or is not usable right now.

        Used to explain a 503 to the caller: "no available accounts" alone does
        not say whether the pool is rate-limited, cooling down after failures,
        or failing to authenticate. Account IDs are reduced to short digests so
        credential paths never reach a client.

        Args:
            exclude_accounts: Account IDs already tried in the current request

        Returns:
            Semicolon-separated per-account state, or a note that the pool is empty
        """
        if not self._accounts:
            return "no accounts configured"

        now = time.time()
        parts: List[str] = []

        for account_id, account in self._accounts.items():
            label = account_label(account_id)

            if exclude_accounts and account_id in exclude_accounts:
                parts.append(f"{label}: already tried in this request")
                continue

            state, seconds = account_routing_state(account, now)

            if state == "auth_dead":
                # Names the remedy, because unlike a suspension this one is the
                # operator's to fix: no amount of waiting renews a rejected token.
                parts.append(f"{label}: credential rejected by the auth host; re-login required")
            elif state == "suspended":
                # Without this the hardest exclusion of all reported "available",
                # sending an operator to debug the pool instead of the account.
                parts.append(f"{label}: suspended upstream; Kiro support must restore it")
            elif state == "quota_exhausted":
                # Phrased in parallel with quota_depleted below: same condition,
                # different evidence. This one was refused upstream.
                parts.append(f"{label}: monthly quota exhausted, excluded for {_format_duration(seconds)}")
            elif state == "quota_depleted":
                if seconds > 0:
                    parts.append(f"{label}: monthly quota spent, excluded for {_format_duration(seconds)}")
                else:
                    parts.append(f"{label}: monthly quota spent, excluded until it resets")
            elif state == "rate_limited":
                parts.append(f"{label}: rate limited for {_format_duration(seconds)}")
            elif state == "cooling_down":
                parts.append(
                    f"{label}: cooling down for {_format_duration(seconds)} "
                    f"after {account.failures} consecutive failure(s)"
                )
            elif state == "uninitialized":
                parts.append(f"{label}: not initialized")
            else:
                parts.append(f"{label}: available")

        return "; ".join(parts)

    def get_first_account(self) -> Account:
        """
        Get first initialized account (for legacy mode).

        Returns:
            First initialized account

        Raises:
            RuntimeError: If no initialized accounts available
        """
        for account in self._accounts.values():
            if account.auth_manager is not None:
                return account
        raise RuntimeError("No initialized accounts available")

    def get_all_available_models(self) -> List[str]:
        """
        Collect unique models from all initialized accounts.

        Used by /v1/models endpoint in account system to show
        all available models across all accounts.

        Returns:
            Sorted list of unique model IDs
        """
        all_models = set()
        for account in self._accounts.values():
            if account.model_resolver:
                all_models.update(account.model_resolver.get_available_models())
        return sorted(all_models)
