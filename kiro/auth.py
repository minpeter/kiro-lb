# -*- coding: utf-8 -*-
"""
Authentication manager for Kiro API.

Manages the lifecycle of access tokens:
- Loading credentials from .env or JSON file
- Automatic token refresh on expiration
- Thread-safe refresh using asyncio.Lock
- Support for both Kiro Desktop Auth and AWS SSO OIDC (kiro-cli)
"""

import asyncio
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger

from kiro.config import (
    TOKEN_REFRESH_THRESHOLD,
    get_aws_sso_oidc_url,
    get_kiro_api_host,
    get_kiro_q_host,
    get_kiro_refresh_url,
)
from kiro.utils import get_machine_fingerprint

# Supported SQLite token keys (searched in priority order)
SQLITE_TOKEN_KEYS = [
    "kirocli:social:token",  # Social login (Google, GitHub, Microsoft, etc.)
    "kirocli:odic:token",  # AWS SSO OIDC (kiro-cli corporate)
    "codewhisperer:odic:token",  # Legacy AWS SSO OIDC
]

# Device registration keys (for AWS SSO OIDC only)
SQLITE_REGISTRATION_KEYS = [
    "kirocli:odic:device-registration",
    "codewhisperer:odic:device-registration",
]


class AuthType(Enum):
    """
    Type of authentication mechanism.

    KIRO_DESKTOP: Kiro IDE credentials (default)
        - Uses https://prod.{region}.auth.desktop.kiro.dev/refreshToken
        - JSON body: {"refreshToken": "..."}

    AWS_SSO_OIDC: AWS SSO credentials from kiro-cli
        - Uses https://oidc.{region}.amazonaws.com/token
        - Form body: grant_type=refresh_token&client_id=...&client_secret=...&refresh_token=...
        - Requires clientId and clientSecret from credentials file
    """

    KIRO_DESKTOP = "kiro_desktop"
    AWS_SSO_OIDC = "aws_sso_oidc"


class KiroAuthManager:
    """
    Manages the token lifecycle for accessing Kiro API.

    Supports:
    - Loading credentials from .env or JSON file
    - Automatic token refresh on expiration
    - Expiration time validation (expiresAt)
    - Saving updated tokens to file
    - Both Kiro Desktop Auth and AWS SSO OIDC (kiro-cli) authentication

    Attributes:
        profile_arn: AWS CodeWhisperer profile ARN
        region: AWS region
        api_host: API host for the current region
        q_host: Q API host for the current region
        fingerprint: Unique machine fingerprint
        auth_type: Type of authentication (KIRO_DESKTOP or AWS_SSO_OIDC)

    Example:
        >>> # Kiro Desktop Auth (default)
        >>> auth_manager = KiroAuthManager(
        ...     refresh_token="your_refresh_token",
        ...     region="us-east-1"
        ... )
        >>> token = await auth_manager.get_access_token()

        >>> # AWS SSO OIDC (kiro-cli) - auto-detected from credentials file
        >>> auth_manager = KiroAuthManager(
        ...     creds_file="~/.aws/sso/cache/your-cache.json"
        ... )
        >>> token = await auth_manager.get_access_token()
    """

    def __init__(
        self,
        refresh_token: Optional[str] = None,
        profile_arn: Optional[str] = None,
        region: str = "us-east-1",
        creds_file: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        sqlite_db: Optional[str] = None,
        api_region: Optional[str] = None,
        internal_account_id: Optional[str] = None,
    ):
        """
        Initializes the authentication manager.

        Args:
            refresh_token: Refresh token for obtaining access token
            profile_arn: AWS CodeWhisperer profile ARN
            region: AWS region (default: us-east-1)
            creds_file: Path to JSON file with credentials (optional)
            client_id: OAuth client ID (for AWS SSO OIDC, optional)
            client_secret: OAuth client secret (for AWS SSO OIDC, optional)
            sqlite_db: Path to kiro-cli SQLite database (optional)
                       Default location: ~/.local/share/kiro-cli/data.sqlite3
            api_region: Q API region override (optional, per-account)
                       If not specified, uses auto-detection or falls back to region
        """
        self._refresh_token = refresh_token
        self._profile_arn = profile_arn
        self._region = region
        self._creds_file = creds_file
        self._sqlite_db = sqlite_db
        self._internal_account_id = internal_account_id

        # AWS SSO OIDC specific fields
        self._client_id: Optional[str] = client_id
        self._client_secret: Optional[str] = client_secret
        self._scopes: Optional[list] = None  # OAuth scopes for AWS SSO OIDC
        self._sso_region: Optional[str] = None  # SSO region for OIDC token refresh (may differ from API region)

        # Enterprise Kiro IDE specific fields
        self._client_id_hash: Optional[str] = None  # clientIdHash from Enterprise Kiro IDE

        # Auto-detected API region from credentials
        # This is separate from SSO region because q.amazonaws.com endpoints
        # only exist in specific regions, while OIDC endpoints exist everywhere
        self._detected_api_region: Optional[str] = None

        # Track which SQLite key we loaded credentials from (for saving back to correct location)
        self._sqlite_token_key: Optional[str] = None

        self._access_token: Optional[str] = None
        self._expires_at: Optional[datetime] = None
        self._lock = asyncio.Lock()

        # Auth type will be determined after loading credentials
        self._auth_type: AuthType = AuthType.KIRO_DESKTOP

        # Fingerprint for User-Agent
        self._fingerprint = get_machine_fingerprint()

        # Load credentials from SQLite if specified (takes priority over JSON)
        if internal_account_id:
            from kiro.store import load_internal_credential

            self._load_credentials_document(load_internal_credential(internal_account_id) or {})
        elif sqlite_db:
            self._load_credentials_from_sqlite(sqlite_db)
        # Load credentials from JSON file if specified
        elif creds_file:
            self._load_credentials_from_file(creds_file)

        # External credential stores are immutable inputs.  A token rotated by
        # this gateway is kept in its private database and takes precedence on
        # subsequent process starts.
        if creds_file or sqlite_db:
            self._load_gateway_overlay()

        # Determine auth type based on available credentials
        self._detect_auth_type()

        # Determine final API region with priority hierarchy:
        # 1. Explicit api_region parameter (per-account) - HIGHEST
        # 2. KIRO_API_REGION env var (global override)
        # 3. Auto-detected from credentials (SQLite ARN or JSON region)
        # 4. SSO region (fallback)
        # 5. Default region parameter (us-east-1)
        api_region_override = os.getenv("KIRO_API_REGION")

        if api_region:
            # Explicit per-account override
            final_api_region = api_region
            logger.info(f"API region: {final_api_region} (from account config)")
        elif api_region_override:
            # Global env var override
            final_api_region = api_region_override
            logger.info(f"API region: {final_api_region} (from KIRO_API_REGION env var)")
        elif self._detected_api_region:
            # Auto-detected from credentials (SQLite profile ARN or JSON region field)
            final_api_region = self._detected_api_region
            logger.info(f"API region: {final_api_region} (auto-detected from credentials)")
        elif self._sso_region:
            # Fallback to SSO region
            final_api_region = self._sso_region
            logger.info(f"API region: {final_api_region} (using SSO region as fallback)")
        else:
            # Final fallback to default region
            final_api_region = region
            logger.info(f"API region: {final_api_region} (using default)")

        # Set up URLs with correct regions:
        # - OIDC refresh: uses SSO region (for token refresh)
        # - API/Q hosts: use determined API region (for Q Developer API calls)
        #
        # An account using SSO OIDC with no profile ARN is AWS Builder ID, which
        # the runtime host rejects with 400 "profileArn is required for this
        # request". Auth type and credentials are both resolved by this point.
        is_builder_id = self._auth_type == AuthType.AWS_SSO_OIDC and not self._profile_arn
        sso_region_for_oidc = self._sso_region or region
        self._refresh_url = get_kiro_refresh_url(sso_region_for_oidc)
        self._api_host = get_kiro_api_host(final_api_region, is_builder_id)
        self._q_host = get_kiro_q_host(final_api_region, is_builder_id)

        # Log initialized endpoints for diagnostics (helps with DNS issues like #58, #132, #133)
        logger.info(
            f"Auth manager initialized: "
            f"sso_region={sso_region_for_oidc}, "
            f"api_region={final_api_region}, "
            f"auth_type={self._auth_type.name}, "
            f"profile_arn={'present' if self._profile_arn else 'absent'}, "
            f"api_host={self._api_host}, "
            f"q_host={self._q_host}"
        )

    def _detect_auth_type(self) -> None:
        """
        Detects authentication type based on available credentials.

        AWS SSO OIDC credentials contain clientId and clientSecret.
        Kiro Desktop credentials do not contain these fields.
        """
        if self._client_id and self._client_secret:
            self._auth_type = AuthType.AWS_SSO_OIDC
            logger.info("Detected auth type: AWS SSO OIDC (kiro-cli)")
        else:
            self._auth_type = AuthType.KIRO_DESKTOP
            logger.info("Detected auth type: Kiro Desktop")

    def _load_credentials_from_sqlite(self, db_path: str, *, apply_overlay: bool = True) -> None:
        """
        Loads credentials from kiro-cli SQLite database.

        The database contains an auth_kv table with key-value pairs.
        Supports multiple authentication types:

        Token keys (searched in priority order):
        - 'kirocli:social:token': Social login (Google, GitHub, etc.)
        - 'kirocli:odic:token': AWS SSO OIDC (kiro-cli corporate)
        - 'codewhisperer:odic:token': Legacy AWS SSO OIDC

        Device registration keys (for AWS SSO OIDC only):
        - 'kirocli:odic:device-registration': Client ID and secret
        - 'codewhisperer:odic:device-registration': Legacy format

        The method remembers which key was used for loading, so credentials
        can be saved back to the correct location after refresh.

        Args:
            db_path: Path to SQLite database file
        """
        try:
            path = Path(db_path).expanduser()
            if not path.exists():
                logger.warning(f"SQLite database not found: {db_path}")
                return

            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            cursor = conn.cursor()

            # Try all possible token keys in priority order
            token_row = None
            for key in SQLITE_TOKEN_KEYS:
                cursor.execute("SELECT value FROM auth_kv WHERE key = ?", (key,))
                token_row = cursor.fetchone()
                if token_row:
                    self._sqlite_token_key = key  # Remember which key we loaded from
                    logger.debug(f"Loaded credentials from SQLite key: {key}")
                    break

            if token_row:
                token_data = json.loads(token_row[0])
                if token_data:
                    # Load token fields (using snake_case as in Rust struct)
                    if "access_token" in token_data:
                        self._access_token = token_data["access_token"]
                    if "refresh_token" in token_data:
                        self._refresh_token = token_data["refresh_token"]
                    if "profile_arn" in token_data:
                        self._profile_arn = token_data["profile_arn"]
                    if "region" in token_data:
                        # Store SSO region for OIDC token refresh
                        # Note: API region is determined separately (see __init__ for priority logic)
                        self._sso_region = token_data["region"]
                        logger.debug(f"SSO region from SQLite: {self._sso_region}")

                    # Load scopes if available
                    if "scopes" in token_data:
                        self._scopes = token_data["scopes"]

                    # Parse expires_at (RFC3339 format)
                    if "expires_at" in token_data:
                        try:
                            expires_str = token_data["expires_at"]
                            # Handle various ISO 8601 formats
                            if expires_str.endswith("Z"):
                                expires_str = expires_str.replace("Z", "+00:00")
                            # Python 3.10 fromisoformat supports max 6 decimal places (microseconds)
                            # kiro-cli writes nanoseconds (9 digits) — truncate to 6
                            expires_str = re.sub(r"(\.\d{6})\d+", r"\1", expires_str)
                            self._expires_at = datetime.fromisoformat(expires_str)
                        except Exception as e:
                            logger.warning(f"Failed to parse expires_at from SQLite: {e}")

            # Load device registration (client_id, client_secret) - try all possible keys
            registration_row = None
            for key in SQLITE_REGISTRATION_KEYS:
                cursor.execute("SELECT value FROM auth_kv WHERE key = ?", (key,))
                registration_row = cursor.fetchone()
                if registration_row:
                    logger.debug(f"Loaded device registration from SQLite key: {key}")
                    break

            if registration_row:
                registration_data = json.loads(registration_row[0])
                if registration_data:
                    if "client_id" in registration_data:
                        self._client_id = registration_data["client_id"]
                    if "client_secret" in registration_data:
                        self._client_secret = registration_data["client_secret"]
                    # SSO region from registration (fallback if not in token data)
                    if "region" in registration_data and not self._sso_region:
                        self._sso_region = registration_data["region"]
                        logger.debug(f"SSO region from device-registration: {self._sso_region}")

            # Try to auto-detect API region from profile ARN in state table
            # This is separate from SSO region because q.amazonaws.com endpoints
            # only exist in specific regions (Issue #132, #133)
            try:
                cursor.execute("SELECT value FROM state WHERE key = 'api.codewhisperer.profile'")
                profile_row = cursor.fetchone()
                if profile_row:
                    profile_data = json.loads(profile_row[0])
                    arn = profile_data.get("arn", "")
                    if arn:
                        if not self._profile_arn:
                            self._profile_arn = arn
                            logger.debug(f"Profile ARN from state table: {self._profile_arn}")
                        # ARN format: arn:aws:codewhisperer:REGION:account:profile/id
                        # Extract region from 4th component (index 3)
                        parts = arn.split(":")
                        if len(parts) >= 4 and parts[3]:
                            # Validate region format (e.g., us-east-1, eu-central-1)
                            if re.match(r"^[a-z]+-[a-z]+-\d+$", parts[3]):
                                self._detected_api_region = parts[3]
                                logger.info(f"API region auto-detected from profile ARN: {parts[3]}")
                            else:
                                logger.debug(f"Invalid region format in ARN: {parts[3]}")
            except sqlite3.Error as e:
                logger.debug(f"Failed to read state table from SQLite: {e}")
            except json.JSONDecodeError as e:
                logger.debug(f"Failed to parse profile data from state table: {e}")
            except Exception as e:
                logger.debug(f"Failed to auto-detect API region from profile ARN: {e}")

            conn.close()
            if apply_overlay:
                self._load_gateway_overlay()
            logger.info(f"Credentials loaded from SQLite database: {db_path}")

        except sqlite3.Error as e:
            logger.error(f"SQLite error loading credentials: {e}")
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error in SQLite data: {e}")
        except Exception as e:
            logger.error(f"Error loading credentials from SQLite: {e}")

    def _load_credentials_from_file(self, file_path: str, *, apply_overlay: bool = True) -> None:
        """
        Loads credentials from a JSON file.

        Supported JSON fields (Kiro Desktop):
        - refreshToken: Refresh token
        - accessToken: Access token (if already available)
        - profileArn: Profile ARN
        - region: AWS region
        - expiresAt: Token expiration time (ISO 8601)

        Additional fields for AWS SSO OIDC (kiro-cli):
        - clientId: OAuth client ID
        - clientSecret: OAuth client secret

        For Enterprise Kiro IDE:
        - clientIdHash: Hash of client ID (Enterprise Kiro IDE)
        - When clientIdHash is present, automatically loads clientId and clientSecret
          from ~/.aws/sso/cache/{clientIdHash}.json (device registration file)

        Args:
            file_path: Path to JSON file
        """
        try:
            path = Path(file_path).expanduser()
            if not path.exists():
                logger.warning(f"Credentials file not found: {file_path}")
                return

            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            self._load_credentials_document(data)
            if apply_overlay:
                self._load_gateway_overlay()

            logger.info(f"Credentials loaded from {file_path}")

        except Exception as e:
            logger.error(f"Error loading credentials from file: {e}")

    def _load_credentials_document(self, data: dict) -> None:
        # Load common credential fields from JSON or the internal store.
        if "refreshToken" in data:
            self._refresh_token = data["refreshToken"]
        if "accessToken" in data:
            self._access_token = data["accessToken"]
        if "profileArn" in data:
            self._profile_arn = data["profileArn"]
        if "region" in data:
            # Store as SSO region for OIDC token refresh
            self._sso_region = data["region"]
            # Also use as detected API region (can be overridden by KIRO_API_REGION env var)
            self._detected_api_region = data["region"]
            logger.debug(f"Region from JSON credentials: {data['region']}")

        # Load clientIdHash and device registration for Enterprise Kiro IDE
        if "clientIdHash" in data:
            self._client_id_hash = data["clientIdHash"]
            self._load_enterprise_device_registration(self._client_id_hash)

        # Load AWS SSO OIDC specific fields (if directly in credentials file)
        if "clientId" in data:
            self._client_id = data["clientId"]
        if "clientSecret" in data:
            self._client_secret = data["clientSecret"]

        # Parse expiresAt
        if "expiresAt" in data:
            try:
                expires_str = data["expiresAt"]
                # Support for different date formats
                if expires_str.endswith("Z"):
                    self._expires_at = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
                else:
                    self._expires_at = datetime.fromisoformat(expires_str)
            except Exception as e:
                logger.warning(f"Failed to parse expiresAt: {e}")

    def _load_enterprise_device_registration(self, client_id_hash: str) -> None:
        """
        Loads clientId and clientSecret from Enterprise Kiro IDE device registration file.

        Enterprise Kiro IDE uses AWS SSO OIDC authentication. Device registration is stored at:
        ~/.aws/sso/cache/{clientIdHash}.json

        Args:
            client_id_hash: Client ID hash used to locate the device registration file
        """
        try:
            device_reg_path = Path.home() / ".aws" / "sso" / "cache" / f"{client_id_hash}.json"

            if not device_reg_path.exists():
                logger.warning(f"Enterprise device registration file not found: {device_reg_path}")
                return

            with open(device_reg_path, "r", encoding="utf-8") as f:
                device_data = json.load(f)

            if "clientId" in device_data:
                self._client_id = device_data["clientId"]

            if "clientSecret" in device_data:
                self._client_secret = device_data["clientSecret"]

            logger.info(f"Enterprise device registration loaded from {device_reg_path}")

        except Exception as e:
            logger.error(f"Error loading enterprise device registration: {e}")

    def _save_credentials_to_file(self) -> None:
        """Persist a rotation without modifying the external JSON input."""
        self._save_gateway_overlay()

    def _external_account_id(self) -> str | None:
        source = self._sqlite_db or self._creds_file
        return str(Path(source).expanduser().resolve()) if source else None

    def _load_gateway_overlay(self) -> None:
        account_id = self._external_account_id()
        if not account_id:
            return
        try:
            from kiro.store import load_internal_credential

            overlay = load_internal_credential(account_id)
            if overlay and self._overlay_is_fresher(overlay):
                self._load_credentials_document(overlay)
                logger.debug("Loaded gateway credential overlay for {}", account_id)
        except Exception as exc:
            logger.debug("No gateway credential overlay for {}: {}", account_id, exc)

    def _overlay_is_fresher(self, overlay: dict) -> bool:
        """Prefer an overlay only when it is demonstrably at least as fresh."""
        overlay_refresh = overlay.get("refreshToken")
        if not overlay_refresh:
            return False
        if overlay_refresh == self._refresh_token:
            return True
        overlay_expiry = self._parse_expiry(overlay.get("expiresAt"))
        if not overlay_expiry or not self._expires_at:
            return False
        return overlay_expiry > self._expires_at

    @staticmethod
    def _parse_expiry(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            return None

    def _reload_raw_external_credentials(self) -> bool:
        """Reload the immutable source, deliberately bypassing its overlay."""
        if self._sqlite_db:
            self._load_credentials_from_sqlite(self._sqlite_db, apply_overlay=False)
            return True
        if self._creds_file:
            self._load_credentials_from_file(self._creds_file, apply_overlay=False)
            return True
        return False

    def _save_gateway_overlay(self) -> None:
        account_id = self._external_account_id()
        if not account_id:
            return
        document = {
            "accessToken": self._access_token,
            "refreshToken": self._refresh_token,
            "expiresAt": self._expires_at.isoformat() if self._expires_at else None,
        }
        if self._profile_arn:
            document["profileArn"] = self._profile_arn
        from kiro.store import connection, require_runtime_writer

        with connection() as conn:
            require_runtime_writer(conn)
            updated = conn.execute(
                "UPDATE account_sources SET credential_json = ? WHERE account_id = ?",
                (json.dumps(document), account_id),
            ).rowcount
        if not updated:
            # Standalone auth-manager consumers have no gateway-owned source row.
            # Writer rejection above remains fatal for managed accounts.
            logger.warning("Could not persist credential overlay for unregistered account {}", account_id)

    def _save_credentials_to_internal_store(self) -> None:
        if not self._internal_account_id:
            return
        from kiro.store import load_internal_credential, save_internal_credential

        document = load_internal_credential(self._internal_account_id) or {}
        document.update(
            accessToken=self._access_token,
            refreshToken=self._refresh_token,
            expiresAt=self._expires_at.isoformat() if self._expires_at else None,
        )
        if self._profile_arn:
            document["profileArn"] = self._profile_arn
        save_internal_credential(self._internal_account_id, document)

    def _save_credentials_to_sqlite(self) -> None:
        """Persist a rotation without modifying the external Kiro CLI database."""
        self._save_gateway_overlay()

    def is_token_expiring_soon(self) -> bool:
        """
        Checks if the token is expiring soon.

        Returns:
            True if the token expires within TOKEN_REFRESH_THRESHOLD seconds
            or if expiration time information is not available
        """
        if not self._expires_at:
            return True  # If no expiration info available, assume refresh is needed

        now = datetime.now(timezone.utc)
        threshold = now.timestamp() + TOKEN_REFRESH_THRESHOLD

        return self._expires_at.timestamp() <= threshold

    def is_token_expired(self) -> bool:
        """
        Checks if the token is actually expired (not just expiring soon).

        This is used for graceful degradation when refresh fails but
        the access token might still be valid for a short time.

        Returns:
            True if the token has already expired or if expiration time
            information is not available
        """
        if not self._expires_at:
            return True  # If no expiration info available, assume expired

        now = datetime.now(timezone.utc)
        return now >= self._expires_at

    async def _refresh_token_request(self) -> None:
        """
        Performs a token refresh request.

        Routes to appropriate refresh method based on auth type:
        - KIRO_DESKTOP: Uses Kiro Desktop Auth endpoint
        - AWS_SSO_OIDC: Uses AWS SSO OIDC endpoint

        Raises:
            ValueError: If refresh token is not set or response doesn't contain accessToken
            httpx.HTTPError: On HTTP request error
        """
        if self._internal_account_id:
            from kiro.store import refresh_internal_credential

            self._load_credentials_document(refresh_internal_credential(self._internal_account_id))
        if self._auth_type == AuthType.AWS_SSO_OIDC:
            await self._refresh_token_aws_sso_oidc()
        else:
            await self._refresh_token_kiro_desktop()

    async def _refresh_token_kiro_desktop(self) -> None:
        try:
            await self._do_kiro_desktop_refresh()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400 and self._reload_raw_external_credentials():
                logger.warning("Token refresh failed with 400; retrying with raw external credentials")
                await self._do_kiro_desktop_refresh()
            else:
                raise

    def _credential_dead_error(self, exc: httpx.HTTPStatusError) -> Exception:
        """Translate a terminal token-endpoint refusal into a typed failure.

        Called only once every in-layer recovery has been tried: the raw-source
        reload and, for a kiro-cli database, the graceful degradation onto a
        still-valid access token. Anything other than the credential-death
        statuses is handed back unchanged, so a 5xx from the auth host keeps its
        transient retry meaning instead of permanently parking the account.
        """
        from kiro.account_errors import CredentialDeadError, is_credential_dead_status

        status = exc.response.status_code
        if not is_credential_dead_status(status):
            return exc
        hint = self._internal_account_id or self._external_account_id() or "refresh_token account"
        logger.error(
            "Refresh token for {} was rejected by the auth host (HTTP {}); "
            "the credential cannot be renewed and needs a re-login.",
            hint,
            status,
        )
        return CredentialDeadError(hint, status)

    async def _do_kiro_desktop_refresh(self) -> None:
        """
        Refreshes token using Kiro Desktop Auth endpoint.

        Endpoint: https://prod.{region}.auth.desktop.kiro.dev/refreshToken
        Method: POST
        Content-Type: application/json
        Body: {"refreshToken": "..."}

        Raises:
            ValueError: If refresh token is not set or response doesn't contain accessToken
            httpx.HTTPError: On HTTP request error
        """
        if not self._refresh_token:
            raise ValueError("Refresh token is not set")

        logger.info("Refreshing Kiro token via Kiro Desktop Auth...")

        payload = {"refreshToken": self._refresh_token}
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"KiroIDE-0.7.45-{self._fingerprint}",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(self._refresh_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        new_access_token = data.get("accessToken")
        new_refresh_token = data.get("refreshToken")
        expires_in = data.get("expiresIn", 3600)
        new_profile_arn = data.get("profileArn")

        if not new_access_token:
            raise ValueError(f"Response does not contain accessToken: {data}")

        # Update data
        self._access_token = new_access_token
        if new_refresh_token:
            self._refresh_token = new_refresh_token
        if new_profile_arn:
            self._profile_arn = new_profile_arn

        # Calculate expiration time with buffer (minus 60 seconds)
        self._expires_at = datetime.now(timezone.utc).replace(microsecond=0)
        self._expires_at = datetime.fromtimestamp(self._expires_at.timestamp() + expires_in - 60, tz=timezone.utc)

        logger.info(f"Token refreshed via Kiro Desktop Auth, expires: {self._expires_at.isoformat()}")

        # Save to file or SQLite depending on configuration
        if self._internal_account_id:
            self._save_credentials_to_internal_store()
        elif self._sqlite_db:
            self._save_credentials_to_sqlite()
        else:
            self._save_credentials_to_file()

    async def _refresh_token_aws_sso_oidc(self) -> None:
        """
        Refreshes token using AWS SSO OIDC endpoint.

        Used by kiro-cli which authenticates via AWS IAM Identity Center.

        Strategy: Try with current in-memory token first. If it fails with 400
        (invalid_request - token was invalidated by kiro-cli re-login), reload
        credentials from SQLite and retry once.

        This approach handles both scenarios:
        1. Container successfully refreshed token (uses in-memory token)
        2. kiro-cli re-login invalidated token (reloads from SQLite on failure)

        Endpoint: https://oidc.{region}.amazonaws.com/token
        Method: POST
        Content-Type: application/x-www-form-urlencoded
        Body: grant_type=refresh_token&client_id=...&client_secret=...&refresh_token=...

        Raises:
            ValueError: If required credentials are not set
            httpx.HTTPError: On HTTP request error
        """
        try:
            await self._do_aws_sso_oidc_refresh()
        except httpx.HTTPStatusError as e:
            # 400 = invalid_request, likely stale token after kiro-cli re-login
            if e.response.status_code == 400 and self._reload_raw_external_credentials():
                logger.warning("Token refresh failed with 400; retrying with raw external credentials")
                await self._do_aws_sso_oidc_refresh()
            else:
                raise

    async def _do_aws_sso_oidc_refresh(self) -> None:
        """
        Performs the actual AWS SSO OIDC token refresh.

        This is the internal implementation called by _refresh_token_aws_sso_oidc().
        It performs a single refresh attempt with current in-memory credentials.

        Uses AWS SSO OIDC CreateToken API format:
        - Content-Type: application/json (not form-urlencoded)
        - Parameter names: camelCase (clientId, not client_id)
        - Payload: JSON object

        Raises:
            ValueError: If required credentials are not set
            httpx.HTTPStatusError: On HTTP error (including 400 for invalid token)
        """
        if not self._refresh_token:
            raise ValueError("Refresh token is not set")
        if not self._client_id:
            raise ValueError("Client ID is not set (required for AWS SSO OIDC)")
        if not self._client_secret:
            raise ValueError("Client secret is not set (required for AWS SSO OIDC)")

        logger.info("Refreshing Kiro token via AWS SSO OIDC...")

        # AWS SSO OIDC CreateToken API uses JSON with camelCase parameters
        # Use SSO region for OIDC endpoint (may differ from API region)
        sso_region = self._sso_region or self._region
        url = get_aws_sso_oidc_url(sso_region)

        # IMPORTANT: AWS SSO OIDC CreateToken API requires:
        # 1. JSON payload (not form-urlencoded)
        # 2. camelCase parameter names (clientId, not client_id)
        payload = {
            "grantType": "refresh_token",
            "clientId": self._client_id,
            "clientSecret": self._client_secret,
            "refreshToken": self._refresh_token,
        }

        headers = {
            "Content-Type": "application/json",
        }

        # Log request details (without secrets) for debugging
        logger.debug(
            f"AWS SSO OIDC refresh request: url={url}, sso_region={sso_region}, "
            f"api_region={self._region}, client_id={self._client_id[:8]}..."
        )

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=payload, headers=headers)

            # Log response details for debugging (especially on errors)
            if response.status_code != 200:
                error_body = response.text
                logger.error(f"AWS SSO OIDC refresh failed: status={response.status_code}, body={error_body}")
                # Try to parse AWS error for more details
                try:
                    error_json = response.json()
                    error_code = error_json.get("error", "unknown")
                    error_desc = error_json.get("error_description", "no description")
                    logger.error(f"AWS SSO OIDC error details: error={error_code}, description={error_desc}")
                except Exception:
                    pass  # Body wasn't JSON, already logged as text
                response.raise_for_status()

            result = response.json()

        # AWS SSO OIDC CreateToken API returns camelCase fields
        new_access_token = result.get("accessToken")
        new_refresh_token = result.get("refreshToken")
        expires_in = result.get("expiresIn", 3600)

        if not new_access_token:
            raise ValueError(f"AWS SSO OIDC response does not contain accessToken: {result}")

        # Update data
        self._access_token = new_access_token
        if new_refresh_token:
            self._refresh_token = new_refresh_token

        # Calculate expiration time with buffer (minus 60 seconds)
        self._expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)

        logger.info(f"Token refreshed via AWS SSO OIDC, expires: {self._expires_at.isoformat()}")

        # Save to file or SQLite depending on configuration
        if self._internal_account_id:
            self._save_credentials_to_internal_store()
        elif self._sqlite_db:
            self._save_credentials_to_sqlite()
        else:
            self._save_credentials_to_file()

    async def get_access_token(self) -> str:
        """
        Returns a valid access_token, refreshing it if necessary.

        Thread-safe method using asyncio.Lock.
        Automatically refreshes the token if it has expired or is about to expire.

        For SQLite mode (kiro-cli): implements graceful degradation when refresh fails.
        If kiro-cli has been running and refreshing tokens in memory (without persisting
        to SQLite), the refresh_token in SQLite becomes stale. In this case, we fall back
        to using the access_token directly until it actually expires.

        Returns:
            Valid access token

        Raises:
            ValueError: If unable to obtain access token
        """
        async with self._lock:
            # Token is valid and not expiring soon - just return it
            if self._access_token and not self.is_token_expiring_soon():
                return self._access_token

            # SQLite mode: reload credentials first, kiro-cli might have updated them
            if self._sqlite_db and self.is_token_expiring_soon():
                logger.debug("SQLite mode: reloading credentials before refresh attempt")
                self._load_credentials_from_sqlite(self._sqlite_db)
                # Check if reloaded token is now valid
                if self._access_token and not self.is_token_expiring_soon():
                    logger.debug("SQLite reload provided fresh token, no refresh needed")
                    return self._access_token

            # Try to refresh the token
            try:
                await self._refresh_with_store_lease()
            except httpx.HTTPStatusError as e:
                # Graceful degradation for SQLite mode when refresh fails twice
                # This happens when kiro-cli refreshed tokens in memory without persisting
                if e.response.status_code == 400 and self._sqlite_db:
                    logger.warning(
                        "Token refresh failed with 400 after SQLite reload. "
                        "This may happen if kiro-cli refreshed tokens in memory without persisting."
                    )
                    # Check if access_token is still usable
                    if self._access_token and not self.is_token_expired():
                        logger.warning(
                            "Using existing access_token until it expires. "
                            "Run 'kiro-cli login' when convenient to refresh credentials."
                        )
                        return self._access_token
                    else:
                        raise ValueError(
                            "Token expired and refresh failed. Please run 'kiro-cli login' to refresh your credentials."
                        )
                # Every in-layer recovery is spent, so a credential-death status
                # is final. Translate it here rather than letting the raw
                # HTTPStatusError escape: it is neither RequestError nor
                # TimeoutException, so it slipped past every handler in the retry
                # loop and the routes' except HTTPException, becoming a bare 500
                # with no report_failure and leaving a dead account in rotation.
                raise self._credential_dead_error(e) from e
            except Exception:
                # For any other exception, propagate it
                raise

            if not self._access_token:
                raise ValueError("Failed to obtain access token")

            return self._access_token

    async def force_refresh(self) -> str:
        """
        Forces a token refresh.

        Used when receiving a 403 error from the API.

        Returns:
            New access token
        """
        async with self._lock:
            try:
                await self._refresh_with_store_lease(force=True)
            except httpx.HTTPStatusError as e:
                # Same translation as get_access_token: this path runs on a 403
                # from the data plane, where an unconverted HTTPStatusError would
                # escape the retry loop as a 500 instead of failing over.
                raise self._credential_dead_error(e) from e
            assert self._access_token is not None
            return self._access_token

    async def _refresh_with_store_lease(self, *, force: bool = False) -> None:
        """Serialize gateway-owned refreshes across blue/green processes."""
        account_id = self._internal_account_id or self._external_account_id()
        if not account_id:
            await self._refresh_token_request()
            return

        from kiro.store import (
            load_internal_credential,
            release_refresh_lease,
            try_acquire_refresh_lease,
        )

        previous_token = self._access_token
        owner = None
        while owner is None:
            owner = try_acquire_refresh_lease(account_id)
            if owner is None:
                await asyncio.sleep(0.05)
        try:
            latest = load_internal_credential(account_id)
            if latest:
                self._load_credentials_document(latest)
            renewed_elsewhere = self._access_token != previous_token
            if self._access_token and not self.is_token_expiring_soon() and (not force or renewed_elsewhere):
                return
            await self._refresh_token_request()
        finally:
            release_refresh_lease(account_id, owner)

    @property
    def profile_arn(self) -> Optional[str]:
        """AWS CodeWhisperer profile ARN."""
        return self._profile_arn

    @property
    def region(self) -> str:
        """AWS region."""
        return self._region

    @property
    def api_host(self) -> str:
        """API host for the current region."""
        return self._api_host

    @property
    def q_host(self) -> str:
        """Q API host for the current region."""
        return self._q_host

    @property
    def fingerprint(self) -> str:
        """Unique machine fingerprint."""
        return self._fingerprint

    @property
    def auth_type(self) -> AuthType:
        """Authentication type (KIRO_DESKTOP or AWS_SSO_OIDC)."""
        return self._auth_type
