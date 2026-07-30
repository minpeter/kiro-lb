# -*- coding: utf-8 -*-

# Kiro Gateway
# https://github.com/jwadow/kiro-gateway
# Copyright (C) 2025 Jwadow
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
Kiro Gateway Configuration.

Centralized storage for all settings, constants, and mappings.
Loads environment variables and provides typed access to them.
"""

import os
import re
from pathlib import Path
from typing import Dict, List, Optional
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


def _get_raw_env_value(var_name: str, env_file: str = ".env") -> Optional[str]:
    """
    Read variable value from .env file without processing escape sequences.
    
    This is necessary for correct handling of Windows paths where backslashes
    (e.g., D:\\Projects\\file.json) may be incorrectly interpreted
    as escape sequences (\\a -> bell, \\n -> newline, etc.).
    
    Args:
        var_name: Environment variable name
        env_file: Path to .env file (default ".env")
    
    Returns:
        Raw variable value or None if not found
    """
    env_path = Path(env_file)
    if not env_path.exists():
        return None
    
    try:
        # Read file as-is, without interpretation
        content = env_path.read_text(encoding="utf-8")
        
        # Search for variable considering different formats:
        # VAR="value" or VAR='value' or VAR=value
        # Pattern captures value with or without quotes
        pattern = rf'^{re.escape(var_name)}=(["\']?)(.+?)\1\s*$'
        
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("#") or not line:
                continue
            
            match = re.match(pattern, line)
            if match:
                # Return value as-is, without processing escape sequences
                return match.group(2)
    except Exception:
        pass
    
    return None

# ==================================================================================================
# Server Settings
# ==================================================================================================

# Server host (default: 0.0.0.0 - listen on all interfaces)
# Use "127.0.0.1" to only allow local connections
DEFAULT_SERVER_HOST: str = "0.0.0.0"
SERVER_HOST: str = os.getenv("SERVER_HOST", DEFAULT_SERVER_HOST)

# Server port (default: 8000)
# Can be overridden by CLI: python main.py --port 9000
# Or by uvicorn directly: uvicorn main:app --port 9000
DEFAULT_SERVER_PORT: int = 8000
SERVER_PORT: int = int(os.getenv("SERVER_PORT", str(DEFAULT_SERVER_PORT)))

# ==================================================================================================
# Proxy Server Settings
# ==================================================================================================

# API key for proxy access (clients must pass it in Authorization header)
PROXY_API_KEY: str = os.getenv("PROXY_API_KEY", "my-super-secret-password-123")

# ==================================================================================================
# VPN/Proxy Settings for Kiro API Access
# ==================================================================================================

# VPN/Proxy URL for accessing Kiro API through a proxy server.
# Leave empty to connect directly (default).
#
# Use cases:
#   - China: GFW (Great Firewall) blocks AWS endpoints
#   - Corporate networks: Often require mandatory proxy
#   - Privacy: Hide your IP address from AWS
#
# Supports HTTP and SOCKS5 protocols.
# Authentication can be embedded in the URL.
#
# Examples:
#   VPN_PROXY_URL=http://127.0.0.1:7890
#   VPN_PROXY_URL=socks5://127.0.0.1:1080
#   VPN_PROXY_URL=http://user:password@proxy.company.com:8080
#   VPN_PROXY_URL=192.168.1.100:8080  (defaults to http://)
VPN_PROXY_URL: str = os.getenv("VPN_PROXY_URL", "")

# ==================================================================================================
# Kiro API Credentials
# ==================================================================================================

# Refresh token for updating access token
REFRESH_TOKEN: str = os.getenv("REFRESH_TOKEN", "")

# Profile ARN for AWS CodeWhisperer
PROFILE_ARN: str = os.getenv("PROFILE_ARN", "")

# AWS SSO/auth region (default us-east-1)
# This region is used for OIDC token refresh endpoint: https://oidc.{region}.amazonaws.com/token
#
# IMPORTANT: SSO region may differ from Q API region!
# - SSO region: Where your AWS SSO/IAM Identity Center is configured
# - API region: Where Q Developer API endpoints are available (q.{region}.amazonaws.com)
#
# The gateway automatically detects the correct API region from your credentials:
# - SQLite (kiro-cli): Extracts from profile ARN in state table
# - JSON (Kiro IDE): Uses region field from credentials file
# - Environment variables: Falls back to this SSO region
#
# For manual override of API region, use KIRO_API_REGION environment variable.
# See: https://github.com/jwadow/kiro-gateway/issues/132
REGION: str = os.getenv("KIRO_REGION", "us-east-1")

# Path to credentials file (optional, alternative to .env)
# Read directly from .env to avoid escape sequence issues on Windows
# (e.g., \a in path D:\Projects\adolf is interpreted as bell character)
_raw_creds_file = _get_raw_env_value("KIRO_CREDS_FILE") or os.getenv("KIRO_CREDS_FILE", "")
# Normalize path for cross-platform compatibility
KIRO_CREDS_FILE: str = str(Path(_raw_creds_file)) if _raw_creds_file else ""

# Path to kiro-cli SQLite database (optional, for AWS SSO OIDC authentication)
# Default location: ~/.local/share/kiro-cli/data.sqlite3 (Linux/macOS)
# or ~/.local/share/amazon-q/data.sqlite3 (amazon-q-developer-cli)
_raw_cli_db_file = _get_raw_env_value("KIRO_CLI_DB_FILE") or os.getenv("KIRO_CLI_DB_FILE", "")
KIRO_CLI_DB_FILE: str = str(Path(_raw_cli_db_file)) if _raw_cli_db_file else ""

# Disable SQLite write-back (read-only mode)
# When enabled, gateway will only read from kiro-cli database without modifying it.
# Useful when kiro-cli is actively managing tokens and you don't want gateway to interfere.
# Default: false (write-back enabled)
SQLITE_READONLY: bool = os.getenv("SQLITE_READONLY", "false").lower() in ("true", "1", "yes")

# ==================================================================================================
# Kiro API URL Templates
# ==================================================================================================

# URL for token refresh (Kiro Desktop Auth)
KIRO_REFRESH_URL_TEMPLATE: str = "https://prod.{region}.auth.desktop.kiro.dev/refreshToken"

# URL for token refresh (AWS SSO OIDC - used by kiro-cli)
AWS_SSO_OIDC_URL_TEMPLATE: str = "https://oidc.{region}.amazonaws.com/token"

# Host for main API (generateAssistantResponse)
# Universal endpoint for all regions (us-east-1, eu-central-1, etc.)
# See: https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/security-data-perimeter.html
# Fixed in issue #58 - codewhisperer.{region}.amazonaws.com doesn't exist for non-us-east-1 regions
KIRO_API_HOST_TEMPLATE: str = "https://runtime.{region}.kiro.dev"

# Host for Q API (ListAvailableModels)
KIRO_Q_HOST_TEMPLATE: str = "https://runtime.{region}.kiro.dev"

# Host for accounts that reach Q Developer without a profile, which in practice
# means AWS Builder ID (SSO OIDC auth that has no profile ARN). The runtime host
# rejects those with 400 "profileArn is required for this request", and Builder ID
# cannot obtain a profile at all: ListAvailableProfiles answers 403 and an empty
# profileArn fails as REQUEST_BODY_INVALID.
#
# Social accounts are a different case: they normally carry a profile, but one
# configured without it still belongs on the runtime host, so absence of a profile
# alone is not the test.
KIRO_BUILDER_ID_HOST_TEMPLATE: str = "https://q.{region}.amazonaws.com"

# ==================================================================================================
# Token Settings
# ==================================================================================================

# Time before token expiration when refresh is needed (in seconds)
# Default 10 minutes - refresh token in advance to avoid errors
TOKEN_REFRESH_THRESHOLD: int = 600

# ==================================================================================================
# Retry Configuration
# ==================================================================================================

# Maximum number of retry attempts on errors
MAX_RETRIES: int = 3

# Base delay between attempts (seconds)
# Uses exponential backoff: delay * (2 ** attempt)
BASE_RETRY_DELAY: float = 1.0

# ==================================================================================================
# Hidden Models Configuration
# ==================================================================================================

# Hidden models - not returned by Kiro /ListAvailableModels API but still functional.
# These ARE shown in our /v1/models endpoint!
# Use dot format for consistency with API models.
#
# Format: "display_name" → "internal_kiro_id"
# Display names use dots (e.g., "claude-3.7-sonnet") for consistency with Kiro API.
#
# Why "hidden"? These models work but are not advertised by Kiro's /ListAvailableModels.
# We expose them to our users because they're useful.
HIDDEN_MODELS: Dict[str, str] = {
    # Claude 3.7 Sonnet - legacy model, maps to "auto" on new runtime endpoint
    # "claude-3.7-sonnet": "auto",
}

# ==================================================================================================
# Model Aliases Configuration
# ==================================================================================================

# Model aliases - custom names that map to real model IDs.
# This feature allows creating alternative names for models to avoid namespace conflicts
# with IDE-specific model names (e.g., Cursor's "auto" model).
#
# Format: {"alias_name": "real_model_id"}
# - alias_name: The name that will appear in /v1/models and can be used in requests
# - real_model_id: The actual model ID that will be sent to Kiro API
#
# Use cases:
# - Avoid conflicts with IDE-specific model names (e.g., Cursor's "auto")
# - Create user-friendly shortcuts (e.g., "my-opus" → "claude-opus-4.5")
# - Support legacy model names from other providers
#
# Example:
#   MODEL_ALIASES = {
#       "auto-kiro": "auto",
#       "my-opus": "claude-opus-4.5",
#       "gpt-5": "claude-sonnet-4.5"
#   }
#
# Default: {"auto-kiro": "auto"} to avoid Cursor IDE conflict
MODEL_ALIASES: Dict[str, str] = {
    "auto-kiro": "auto",  # Default alias to avoid Cursor's "auto" model conflict
}

# Models to hide from /v1/models endpoint.
# These models still work when requested directly, but are not shown in the model list.
# This is useful when you want to show only aliases instead of original model names.
#
# Use case: Hide "auto" from list to show only "auto-kiro" alias, avoiding confusion.
#
# Example:
#   HIDDEN_FROM_LIST = ["auto", "claude-old-model"]
#
# Default: ["auto"] to show only "auto-kiro" alias
HIDDEN_FROM_LIST: List[str] = ["auto"]

# ==================================================================================================
# Fallback Models Configuration (DNS Failure Recovery)
# ==================================================================================================

# Fallback model list - used when /ListAvailableModels API is unreachable.
# This ensures basic functionality even with DNS/network issues.
#
# IMPORTANT: This list represents known models at the time of this gateway version.
# - Some models may not be available on your Kiro plan (e.g., Opus on free tier)
# - New models released after this version won't appear here
# - Update gateway regularly to get the latest model list
FALLBACK_MODELS: List[Dict[str, str]] = [
    {"modelId": "auto"},
    {"modelId": "claude-sonnet-4"},
    {"modelId": "claude-sonnet-4.5"},
    {"modelId": "claude-sonnet-4.6"},
    {"modelId": "claude-haiku-4.5"},
    {"modelId": "claude-opus-4.5"},
    {"modelId": "claude-opus-4.6"},
    {"modelId": "claude-opus-4.7"},
    {"modelId": "claude-opus-4.8"},
    {"modelId": "claude-opus-5"},
    {"modelId": "claude-sonnet-5"},
    {"modelId": "deepseek-3.2"},
    {"modelId": "glm-5"},
    {"modelId": "minimax-m2.1"},
    {"modelId": "minimax-m2.5"},
    {"modelId": "qwen3-coder-next"},
]

# ==================================================================================================
# Model Cache Settings
# ==================================================================================================

# Model cache TTL in seconds (1 hour)
MODEL_CACHE_TTL: int = 3600

# Default maximum number of input tokens
DEFAULT_MAX_INPUT_TOKENS: int = 200000

# ==================================================================================================
# Tool Description Handling (Kiro API Limitations)
# ==================================================================================================

# Kiro API returns 400 "Improperly formed request" error when tool descriptions
# in toolSpecification.description are too long.
#
# Solution: Tool Documentation Reference Pattern
# - If description ≤ limit → keep as is
# - If description > limit:
#   * In toolSpecification.description → reference to system prompt:
#     "[Full documentation in system prompt under '## Tool: {name}']"
#   * In system prompt, a section "## Tool: {name}" with full description is added
#
# The model sees an explicit reference and knows exactly where to find full documentation.

# Maximum length of tool description in characters.
# Descriptions longer than this limit will be moved to system prompt.
# Set to 0 to disable (not recommended - will cause Kiro API errors).
TOOL_DESCRIPTION_MAX_LENGTH: int = int(os.getenv("TOOL_DESCRIPTION_MAX_LENGTH", "10000"))

# ==================================================================================================
# Truncation Recovery Settings
# ==================================================================================================


# ==================================================================================================
# Logging Settings
# ==================================================================================================

# Log level for the application
# Available levels: TRACE, DEBUG, INFO, WARNING, ERROR, CRITICAL
# Default: INFO (recommended for production)
# Set to DEBUG for detailed troubleshooting
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# ==================================================================================================
# First Token Timeout Settings (Streaming Retry)
# ==================================================================================================

# Timeout for waiting for the first token from the model (in seconds).
# If the model doesn't respond within this time, the request will be cancelled and retried.
# This helps handle "stuck" requests when the model takes too long to think.
# Default: 30 seconds (recommended for production)
# Set a lower value (e.g., 10-15) for more aggressive retry.
FIRST_TOKEN_TIMEOUT: float = float(os.getenv("FIRST_TOKEN_TIMEOUT", "15"))

# Read timeout for streaming responses (in seconds).
# This is the maximum time to wait for data between chunks during streaming.
# Should be longer than FIRST_TOKEN_TIMEOUT since the model may pause between chunks
# while "thinking" (especially for tool calls or complex reasoning).
# Default: 300 seconds (5 minutes) - generous timeout to avoid premature disconnects.
STREAMING_READ_TIMEOUT: float = float(os.getenv("STREAMING_READ_TIMEOUT", "300"))

# Maximum number of attempts on first token timeout.
# After exhausting all attempts, an error will be returned.
# Default: 3 attempts
FIRST_TOKEN_MAX_RETRIES: int = int(os.getenv("FIRST_TOKEN_MAX_RETRIES", "3"))

# ==================================================================================================
# Debug Settings
# ==================================================================================================

# Debug logging mode:
# - off: disabled (default)
# - errors: save logs only for failed requests (4xx, 5xx)
# - all: save logs for every request (overwrites on each request)
_DEBUG_MODE_RAW: str = os.getenv("DEBUG_MODE", "").lower()

if _DEBUG_MODE_RAW in ("off", "errors", "all"):
    DEBUG_MODE: str = _DEBUG_MODE_RAW
else:
    DEBUG_MODE: str = "off"

# Directory for debug log files
DEBUG_DIR: str = os.getenv("DEBUG_DIR", "debug_logs")


def _bounded_debug_int(
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Read a bounded debug setting and fail closed to its safe default."""
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    if value < minimum or value > maximum:
        return default
    return value


DEBUG_CAPTURE_CONTENT: bool = (
    os.getenv("DEBUG_CAPTURE_CONTENT", "false").lower() == "true"
)
DEBUG_CAPTURE_SUCCESS: bool = (
    os.getenv("DEBUG_CAPTURE_SUCCESS", "false").lower() == "true"
)
DEBUG_CAPTURE_MAX_BYTES: int = _bounded_debug_int(
    "DEBUG_CAPTURE_MAX_BYTES",
    4 * 1024 * 1024,
    64 * 1024,
    64 * 1024 * 1024,
)
DEBUG_CAPTURE_RETENTION: int = _bounded_debug_int(
    "DEBUG_CAPTURE_RETENTION",
    10,
    1,
    100,
)


def _warn_timeout_configuration():
    """
    Print warning if timeout configuration is suboptimal.
    Called at application startup.
    
    FIRST_TOKEN_TIMEOUT should be less than STREAMING_READ_TIMEOUT:
    - FIRST_TOKEN_TIMEOUT: time to wait for model to START responding
    - STREAMING_READ_TIMEOUT: time to wait BETWEEN chunks during streaming
    """
    if FIRST_TOKEN_TIMEOUT >= STREAMING_READ_TIMEOUT:
        import sys
        YELLOW = "\033[93m"
        RESET = "\033[0m"
        
        warning_text = f"""
{YELLOW}⚠️  WARNING: Suboptimal timeout configuration detected.
    
    FIRST_TOKEN_TIMEOUT ({FIRST_TOKEN_TIMEOUT}s) >= STREAMING_READ_TIMEOUT ({STREAMING_READ_TIMEOUT}s)
    
    These timeouts serve different purposes:
      - FIRST_TOKEN_TIMEOUT: time to wait for model to START responding (default: 15s)
      - STREAMING_READ_TIMEOUT: time to wait BETWEEN chunks during streaming (default: 300s)
    
    Recommendation: FIRST_TOKEN_TIMEOUT should be LESS than STREAMING_READ_TIMEOUT.
    
    Example configuration:
      FIRST_TOKEN_TIMEOUT=15
      STREAMING_READ_TIMEOUT=300{RESET}
"""
        print(warning_text, file=sys.stderr)

# ==================================================================================================
# Payload Size Guard Settings
# ==================================================================================================

# Payload size limit in bytes (Kiro API rejects > ~615KB with cryptic 400 error)
# Default 600KB provides safety margin below the ~615KB hard limit
KIRO_MAX_PAYLOAD_BYTES: int = int(os.getenv("KIRO_MAX_PAYLOAD_BYTES", "600000"))

# Auto-trim payload when over limit (default: false - disabled)
# Enable this if you use many tools (30+) and hit "Improperly formed request" errors
# When false, returns a clear error instead of trimming
AUTO_TRIM_PAYLOAD: bool = os.getenv("AUTO_TRIM_PAYLOAD", "false").lower() in ("true", "1", "yes")

# ==================================================================================================
# WebSearch Settings (MCP Tool Emulation)
# ==================================================================================================

# Enable web_search tool auto-injection (default: true)
# When enabled, web_search is automatically added as a tool for MCP emulation (Path B)
# Model decides whether to use it or not
#
# Note: Native Anthropic server-side tools (Path A) work ALWAYS, regardless of this setting
WEB_SEARCH_ENABLED: bool = os.getenv("WEB_SEARCH_ENABLED", "true").lower() in ("true", "1", "yes")

# ==================================================================================================
# Account System Settings
# ==================================================================================================

# Enable account system with failover (default: false)
# When false: uses first account without failover (legacy mode)
# When true: enables full failover loop with Circuit Breaker
ACCOUNT_SYSTEM: bool = os.getenv("ACCOUNT_SYSTEM", "false").lower() in ("true", "1", "yes")

# Path to credentials configuration file
ACCOUNTS_CONFIG_FILE: str = os.getenv("ACCOUNTS_CONFIG_FILE", "credentials.json")

# Path to runtime state file
ACCOUNTS_STATE_FILE: str = os.getenv("ACCOUNTS_STATE_FILE", "state.json")

# ==================================================================================================
# Circuit Breaker Settings
# ==================================================================================================

# Base recovery timeout in seconds (for exponential backoff)
# Actual timeout = BASE * 2^(failures - 1), capped at BASE * MAX_MULTIPLIER
# Examples with BASE=60s, MAX=1440x:
#   1 failure: 1m, 2: 2m, 3: 4m, 4: 8m, 5: 16m, 6: 32m, 7: 1h, 8: 2h, 9: 4h, 10: 8.5h, 11: 17h, 12+: 1d (cap)
ACCOUNT_RECOVERY_TIMEOUT: int = int(os.getenv("ACCOUNT_RECOVERY_TIMEOUT", "60"))

# Maximum backoff multiplier (cap for exponential backoff)
# With BASE=60s and MAX=1440, maximum cooldown is 60 * 1440 = 86400s = 1 day
ACCOUNT_MAX_BACKOFF_MULTIPLIER: float = float(os.getenv("ACCOUNT_MAX_BACKOFF_MULTIPLIER", "1440.0"))

# Probabilistic retry chance for "broken" accounts (0.0 - 1.0)
# Even if account is broken and timeout hasn't passed, try with this probability
# Default: 0.1 (10% chance) - prevents permanent "stuck" state
ACCOUNT_PROBABILISTIC_RETRY_CHANCE: float = float(os.getenv("ACCOUNT_PROBABILISTIC_RETRY_CHANCE", "0.1"))

# Cooldown in seconds for a request-rate rejection (429 USER_REQUEST_RATE_EXCEEDED).
# A rate rejection means the account was asked too quickly, not that it is broken,
# so it is kept out of the Circuit Breaker: the account rotates out for a few
# seconds and returns at full health. Feeding it into the exponential backoff
# instead escalated a momentary burst into hour-long exclusions and shrank the
# usable pool (observed live: 1m -> 2m -> 4m ... -> 1h within four minutes).
ACCOUNT_RATE_LIMIT_COOLDOWN: int = int(os.getenv("ACCOUNT_RATE_LIMIT_COOLDOWN", "10"))

# Quarantine in seconds for an account whose monthly quota is exhausted
# (402 MONTHLY_REQUEST_COUNT). Such an account cannot serve any request until
# its quota resets, so it leaves the rotation entirely: no probabilistic retry
# reaches it, unlike a Circuit Breaker cooldown. Persisted across restarts
# because the state outlives the process. Default 6h re-checks a few times a
# day, which is enough to notice a reset or a plan upgrade without spending
# live requests on a known-empty account.
ACCOUNT_QUOTA_QUARANTINE: int = int(os.getenv("ACCOUNT_QUOTA_QUARANTINE", "21600"))

# Averaging window for the dashboard's requests-per-minute figure. Kiro rejects
# on instantaneous speed, so the chart measures a sliding count over this many
# seconds at each request instant rather than averaging a whole bucket.
RATE_WINDOW_SECONDS: int = int(os.getenv("RATE_WINDOW_SECONDS", "60"))

# How far back rate observations count toward the inferred limit and how much
# routing history the dashboard chart can show. A bound taken from the lowest
# rejection never rises on its own, so an upstream limit that was raised would
# stay pinned to the old value forever; ageing samples out lets the estimate
# recover. Default 24h keeps enough samples to stay tight while adapting within
# a day.
RATE_ESTIMATE_WINDOW_SECONDS: int = int(os.getenv("RATE_ESTIMATE_WINDOW_SECONDS", "86400"))

# Retention for persisted rate observations, which outlive the estimate window
# so an operator can still inspect history.
RATE_OBSERVATION_RETENTION_DAYS: int = int(os.getenv("RATE_OBSERVATION_RETENTION_DAYS", "7"))

# Retention for the dashboard request log. Without pruning the table grows
# unbounded and the 24h overview aggregate degrades: measured 0.85ms at 10k
# rows versus 19.8ms at 1M, which matters once the dashboard polls every second.
REQUEST_LOG_RETENTION_DAYS: int = int(os.getenv("REQUEST_LOG_RETENTION_DAYS", "7"))

# ==================================================================================================
# Account Cache Settings
# ==================================================================================================

# Model cache TTL in seconds (12 hours)
# Cache is refreshed only when account is used (not in background)
ACCOUNT_CACHE_TTL: int = int(os.getenv("ACCOUNT_CACHE_TTL", "43200"))

# ==================================================================================================
# State Persistence Settings
# ==================================================================================================

# Interval for periodic state.json saving in seconds
STATE_SAVE_INTERVAL_SECONDS: int = int(os.getenv("STATE_SAVE_INTERVAL_SECONDS", "10"))

# Dashboard live usage polling interval. A value of 0 disables background refresh;
# dashboard operators can still request a manual refresh.
USAGE_REFRESH_INTERVAL_SECONDS: int = int(os.getenv("USAGE_REFRESH_INTERVAL_SECONDS", "900"))

# ==================================================================================================
# Application Version
# ==================================================================================================

APP_VERSION: str = "0.1.0"
APP_TITLE: str = "kiro-lb"
APP_DESCRIPTION: str = "Private Kiro API load balancer. OpenAI and Anthropic compatible; never fabricates reasoning content."


def get_kiro_refresh_url(region: str) -> str:
    """Return Kiro Desktop Auth token refresh URL for the specified region."""
    return KIRO_REFRESH_URL_TEMPLATE.format(region=region)


def get_aws_sso_oidc_url(region: str) -> str:
    """Return AWS SSO OIDC token URL for the specified region."""
    return AWS_SSO_OIDC_URL_TEMPLATE.format(region=region)


def get_kiro_api_host(region: str, is_builder_id: bool = False) -> str:
    """Return the API host for the region, routing Builder ID to Q Developer."""
    template = KIRO_BUILDER_ID_HOST_TEMPLATE if is_builder_id else KIRO_API_HOST_TEMPLATE
    return template.format(region=region)


def get_kiro_q_host(region: str, is_builder_id: bool = False) -> str:
    """Return the Q API host for the region, routing Builder ID to Q Developer."""
    template = KIRO_BUILDER_ID_HOST_TEMPLATE if is_builder_id else KIRO_Q_HOST_TEMPLATE
    return template.format(region=region)

