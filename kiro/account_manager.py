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
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
from loguru import logger

from kiro.account_errors import ErrorType
from kiro.auth import AuthType, KiroAuthManager
from kiro.cache import ModelInfoCache
from kiro.config import (
    ACCOUNT_CACHE_TTL,
    ACCOUNT_MAX_BACKOFF_MULTIPLIER,
    ACCOUNT_PROBABILISTIC_RETRY_CHANCE,
    ACCOUNT_QUOTA_QUARANTINE,
    ACCOUNT_RATE_LIMIT_COOLDOWN,
    ACCOUNT_RECOVERY_TIMEOUT,
    ACCOUNT_SUSPENSION_QUARANTINE,
    FALLBACK_MODELS,
    HIDDEN_FROM_LIST,
    HIDDEN_MODELS,
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
        "suspended", "quota_exhausted", "rate_limited", "cooling_down",
        "uninitialized", or "available"; the second element is 0 when nothing
        is pending.
    """
    now = time.time() if now is None else now

    # A suspension outranks every other exclusion: the others describe a
    # condition that clears on its own, this one does not.
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

    return ("available", 0)


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
        models_cached_at: Timestamp of last model cache update
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
    models_cached_at: float = 0.0
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
    - Load credentials from credentials.json
    - Lazy initialization of accounts
    - Select next available account (Circuit Breaker + Sticky)
    - Track statistics and failures
    - Persist state to state.json

    Example:
        >>> manager = AccountManager("credentials.json", "state.json")
        >>> await manager.load_credentials()
        >>> await manager.load_state()
        >>> account = await manager.get_next_account("claude-opus-4.5")
        >>> await manager.report_success(account.id, "claude-opus-4.5")
    """

    def __init__(self, credentials_file: str, state_file: str):
        """
        Initialize AccountManager.

        Args:
            credentials_file: Path to credentials.json
            state_file: Path to state.json
        """
        self._credentials_file = credentials_file
        self._state_file = state_file
        self._accounts: Dict[str, Account] = {}
        self._model_to_accounts: Dict[str, ModelAccountList] = {}
        self._lock = asyncio.Lock()
        self._dirty = False
        self._credentials_config: List[Dict] = []
        self._current_account_index: int = 0  # GLOBAL sticky index for all models
        # Bounded ring of recent routing outcomes for the dashboard rate chart.
        # Memory-only: this is observability, not state the router depends on.
        # Routing history outlives the process; see RateObservation.
        self._rate_observations: List[RateObservation] = []
        self._unsaved_rate_observations: List[RateObservation] = []

    async def load_credentials(self) -> None:
        """
        Load credentials from credentials.json.

        Validates each entry and creates Account objects.
        Invalid entries are skipped with warnings.
        Folders are scanned for credential files.
        """
        creds_path = Path(self._credentials_file).expanduser()

        if not creds_path.exists():
            logger.warning(f"Credentials file not found: {self._credentials_file}")
            return

        try:
            with open(creds_path, "r", encoding="utf-8") as f:
                self._credentials_config = json.load(f)
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
        Load runtime state from state.json.

        Restores model_to_accounts mapping and account runtime state.
        Creates empty state if file doesn't exist.
        """
        state_path = Path(self._state_file)

        if not state_path.exists():
            logger.debug("State file not found, starting with empty state")
            return

        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state_data = json.load(f)
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
                    account.models_cached_at = data.get("models_cached_at", 0.0)

                    stats_data = data.get("stats", {})
                    account.stats = AccountStats(
                        total_requests=stats_data.get("total_requests", 0),
                        successful_requests=stats_data.get("successful_requests", 0),
                        failed_requests=stats_data.get("failed_requests", 0),
                    )

            logger.info(f"Loaded state: {len(self._model_to_accounts)} model mappings, {len(self._accounts)} accounts")

        except Exception as e:
            logger.error(f"Failed to load state: {e}")

    async def _save_state(self) -> None:
        """
        Save runtime state to state.json atomically.

        Uses tmp file + rename for atomic write.
        """
        state_data = {
            "current_account_index": self._current_account_index,
            "accounts": {
                account_id: {
                    "failures": account.failures,
                    "last_failure_time": account.last_failure_time,
                    "quota_exhausted_until": account.quota_exhausted_until,
                    "suspended_until": account.suspended_until,
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

        state_path = Path(self._state_file)
        tmp_path = state_path.with_suffix(".json.tmp")

        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state_data, f, indent=2, ensure_ascii=False)

            # Atomic rename
            tmp_path.replace(state_path)
            logger.debug("State saved successfully")

        except Exception as e:
            logger.error(f"Failed to save state: {e}")
            if tmp_path.exists():
                tmp_path.unlink()

    async def save_state_periodically(self) -> None:
        """
        Background task for periodic state saving.

        Saves state every STATE_SAVE_INTERVAL_SECONDS if dirty flag is set.
        """
        while True:
            await asyncio.sleep(STATE_SAVE_INTERVAL_SECONDS)

            if self._dirty:
                async with self._lock:
                    await self._save_state()
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
        account = self._accounts.get(account_id)
        if not account:
            return False

        try:
            # Find credentials config for this account
            creds_config = None
            for entry in self._credentials_config:
                path = entry.get("path", "")
                expanded_path = Path(path).expanduser()

                if entry.get("type") == "refresh_token":
                    # Match by deterministic hash for refresh_token type
                    token = entry.get("refresh_token", "")
                    token_hash = hashlib.sha256(token.encode()).hexdigest()[:16]
                    if account_id == f"refresh_token_{token_hash}":
                        creds_config = entry
                        break
                elif str(expanded_path.resolve()) == account_id or (
                    expanded_path.is_dir() and account_id.startswith(str(expanded_path.resolve()) + os.sep)
                ):
                    creds_config = entry
                    break

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
            else:
                logger.error(f"Unknown credential type: {cred_type}")
                return False

            # Get token to verify credentials
            token = await auth_manager.get_access_token()

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

            # Update account
            account.auth_manager = auth_manager
            account.model_cache = model_cache
            account.model_resolver = model_resolver
            account.models_cached_at = time.time()

            # Update model_to_accounts mapping
            available_models = model_resolver.get_available_models()
            for model in available_models:
                if model not in self._model_to_accounts:
                    self._model_to_accounts[model] = ModelAccountList()
                if account_id not in self._model_to_accounts[model].accounts:
                    self._model_to_accounts[model].accounts.append(account_id)

            logger.info(f"Initialized account: {account_id} ({len(available_models)} models)")
            self._dirty = True
            return True

        except Exception as e:
            logger.error(f"Failed to initialize account {account_id}: {e}")
            return False

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

    async def _refresh_account_models(self, account_id: str) -> None:
        """
        Refresh model cache for account (TTL refresh).

        Args:
            account_id: Account ID to refresh
        """
        account = self._accounts.get(account_id)
        if not account or not account.auth_manager:
            return

        # These dependencies are initialized atomically with the auth manager.
        assert account.model_cache is not None
        assert account.model_resolver is not None

        # Check if using runtime endpoint (no dynamic model list available)
        if _is_runtime_endpoint(account.auth_manager):
            # Runtime endpoint does not provide /ListAvailableModels
            # Use static list and update cache timestamp
            logger.debug(
                f"Account {account_id}: Skipping model refresh for runtime.kiro.dev endpoint (using static list)"
            )
            await account.model_cache.update(FALLBACK_MODELS)
            account.models_cached_at = time.time()
            self._dirty = True
            return

        # Old endpoint - attempt to fetch dynamic model list
        # Use KiroHttpClient for retry logic
        http_client = KiroHttpClient(account.auth_manager, shared_client=None)

        try:
            params = {"origin": "AI_EDITOR"}
            if account.auth_manager.auth_type == AuthType.KIRO_DESKTOP and account.auth_manager.profile_arn:
                params["profileArn"] = account.auth_manager.profile_arn

            list_models_url = f"{account.auth_manager.q_host}/ListAvailableModels"

            response = await http_client.request_with_retry(
                method="GET", url=list_models_url, json_data=None, params=params, stream=False
            )

            if response.status_code != 200:
                # A TTL refresh is also an upstream verdict. Without this check a
                # suspension arriving here was swallowed and the stale cache kept
                # the locked account advertising models indefinitely.
                self._quarantine_if_suspended(account_id, account, response)
            else:
                data = response.json()
                models_list = data.get("models", [])
                await account.model_cache.update(models_list)
                account.models_cached_at = time.time()

                # Update model_to_accounts mapping (new models may have appeared)
                available_models = account.model_resolver.get_available_models()
                for model in available_models:
                    if model not in self._model_to_accounts:
                        self._model_to_accounts[model] = ModelAccountList()
                    if account_id not in self._model_to_accounts[model].accounts:
                        self._model_to_accounts[model].accounts.append(account_id)

                logger.debug(f"Refreshed models for {account_id}")
                self._dirty = True

        except Exception as e:
            # All retries exhausted - keep using stale cache
            logger.warning(f"Failed to refresh models for {account_id} after retries: {e}")

        finally:
            await http_client.close()

    async def get_next_account(self, model: str, exclude_accounts: Optional[set] = None) -> Optional[Account]:
        """
        Get next available account for model (Circuit Breaker + Sticky).

        Implements:
        - Sticky behavior (prefer successful account)
        - Circuit Breaker with exponential backoff
        - Probabilistic retry for "dead" accounts (10%)
        - Short skip for rate-limited accounts (no failure penalty)
        - Full exclusion of quota-exhausted accounts (no probabilistic retry)
        - TTL-based model cache refresh
        - Exclusion of already-tried accounts in current failover loop

        Args:
            model: Model name (will be normalized)
            exclude_accounts: Set of account IDs to exclude (already tried in current failover loop)

        Returns:
            Account object or None if no accounts available
        """
        async with self._lock:
            # Special case: single account - bypass Circuit Breaker
            # Circuit Breaker is meaningless for single account - user should see real Kiro API errors
            # instead of generic "Account unavailable" after cooldown kicks in
            if len(self._accounts) == 1:
                account_id = list(self._accounts.keys())[0]
                account = self._accounts[account_id]

                # Skip if already tried in current failover loop
                if exclude_accounts and account_id in exclude_accounts:
                    return None

                # Lazy initialization if needed
                if account.auth_manager is None:
                    success = await self._initialize_account(account_id)
                    if not success:
                        return None

                # Check TTL and refresh if needed
                if account.models_cached_at > 0:
                    age = time.time() - account.models_cached_at
                    if age > ACCOUNT_CACHE_TTL:
                        try:
                            await self._refresh_account_models(account_id)
                        except Exception as e:
                            logger.warning(f"Failed to refresh models for {account_id}: {e}")
                # # Validate model availability
                # if account.model_resolver:
                #     normalized_model = normalize_model_name(model)
                #     available_models = account.model_resolver.get_available_models()
                #     if normalized_model not in available_models:
                #         return None

                # Always return single account (ignore cooldown/failures)
                # No model validation - let Kiro API decide (gateway, not gatekeeper)
                return account

            # Multi-account logic: GLOBAL sticky
            # ALWAYS start from GLOBAL index (one current account for ALL models)
            start_index = self._current_account_index

            # ALWAYS iterate over ALL accounts
            all_account_ids = list(self._accounts.keys())

            for i in range(len(all_account_ids)):
                current_index = (start_index + i) % len(all_account_ids)
                account_id = all_account_ids[current_index]
                account = self._accounts[account_id]

                # Skip accounts already tried in current failover loop
                if exclude_accounts and account_id in exclude_accounts:
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
                        else:
                            logger.info(f"Probabilistic retry for broken account {account_id}")
                    else:
                        # Half-Open: recovery timeout passed
                        logger.info(
                            f"Half-Open state for {account_id} (recovery timeout passed, effective={effective_timeout}s)"
                        )

                # Lazy initialization
                if account.auth_manager is None:
                    success = await self._initialize_account(account_id)
                    if not success:
                        account.failures += 1
                        self._dirty = True
                        continue

                # Check TTL and refresh if needed
                if account.models_cached_at > 0:
                    age = time.time() - account.models_cached_at
                    if age > ACCOUNT_CACHE_TTL:
                        try:
                            await self._refresh_account_models(account_id)
                        except Exception as e:
                            logger.warning(f"Failed to refresh models for {account_id}: {e}")
                # # Check if model is available on this account
                # available_models = account.model_resolver.get_available_models()
                # if normalized_model not in available_models:
                #     continue

                # No model validation - let Kiro API decide (gateway, not gatekeeper)
                # Account is suitable!
                return account

            # All accounts unavailable
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

            # GLOBAL STICKY: Update global current_account_index
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
                account.quota_exhausted_until = time.time() + ACCOUNT_QUOTA_QUARANTINE
                account.stats.total_requests += 1
                account.stats.failed_requests += 1
                self._record_routing_event(account_id, "quota_exhausted")
                self._dirty = True
                logger.warning(
                    f"Account {account_id} monthly quota exhausted: "
                    f"status={status_code}, reason={reason}, "
                    f"excluded from routing for {_format_duration(ACCOUNT_QUOTA_QUARANTINE)}"
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

            accounts.append(
                {
                    "account": account_label(account_id),
                    "success": success,
                    "rateLimited": rate_limited,
                    "failure": failure,
                    "peakRpm": peak_rpm,
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

            if state == "quota_exhausted":
                parts.append(f"{label}: monthly quota exhausted, excluded for {_format_duration(seconds)}")
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
