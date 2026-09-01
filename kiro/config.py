# -*- coding: utf-8 -*-
"""
Kiro Gateway Configuration.

Centralized storage for all settings, constants, and mappings.
Loads environment variables and provides typed access to them.
"""

import os
from typing import Any, Dict, List

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

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
PROXY_API_KEY: str = os.getenv("PROXY_API_KEY", "")

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
# Defaults (not env-seeded — accounts come from the dashboard / SQLite store)
# ==================================================================================================

# Fallback region when a credential entry does not carry its own region.
REGION: str = "us-east-1"

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

# Legacy Q management host for Builder ID accounts. Current generation requests
# use the runtime host with the request-scoped profile below.
KIRO_BUILDER_ID_HOST_TEMPLATE: str = "https://q.{region}.amazonaws.com"

# Builder ID management and generation requests in Kiro CLI 2.19.1 carry this
# service profile even though the local credential has no account-specific ARN.
# Keep it request-scoped: it is not persisted as the account's own profile.
KIRO_BUILDER_ID_PROFILE_ARN: str = "arn:aws:codewhisperer:us-east-1:638616132270:profile/AAAACCCCXXXX"

# ==================================================================================================
# Token Settings
# ==================================================================================================

# Time before token expiration when refresh is needed (in seconds)
# Default 10 minutes - refresh token in advance to avoid errors.
# The dashboard can override this at runtime; see kiro/gateway_tunables.py.
TOKEN_REFRESH_THRESHOLD: int = int(os.getenv("TOKEN_REFRESH_THRESHOLD", "600"))

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
#
# tokenLimits mirror what /ListAvailableModels reports, because accounts on the
# runtime endpoint never reach that API and would otherwise fall back to
# DEFAULT_MAX_INPUT_TOKENS for every model. Assuming 200k for a 1M model
# understates reported context usage by 5x.
#
# Four values deliberately do NOT mirror the reported figure. claude-opus-4.7,
# claude-opus-4.8, claude-opus-5 and claude-sonnet-5 advertise 1000000, but the
# runtime endpoint charges 1.50x per cl100k token against that number while every
# other model charges 1.00x. Measured slopes, English text, two payload sizes so
# the fixed per-request overhead cancels:
#
#   claude-opus-4.7  1.5018    claude-opus-4.6    0.999
#   claude-opus-4.8  1.4974    claude-sonnet-4.6  1.000
#   claude-opus-5    1.4963    auto               0.999
#   claude-sonnet-5  1.4984    gpt-5.6-sol        0.999
#
# A tokenizer cannot make English denser than Korean (1.158 on the same model), so
# the 1.5x is not tokenization: the real window is two thirds of the advertised
# one. Inverting each slope gives 665853, 667824, 668300 and 667364 - all within
# 0.3% of 666667 - so contextUsagePercentage on these four is a percentage of
# 666667, not of 1000000. Keeping 1000000 here inflated every derived token count
# by 1.5x and made clients compact far too late.
FALLBACK_MODELS: List[Dict[str, Any]] = [
    {"modelId": "auto", "tokenLimits": {"maxInputTokens": 1000000, "maxOutputTokens": 64000}},
    {"modelId": "claude-sonnet-4", "tokenLimits": {"maxInputTokens": 200000, "maxOutputTokens": 64000}},
    {"modelId": "claude-sonnet-4.5", "tokenLimits": {"maxInputTokens": 200000, "maxOutputTokens": 64000}},
    {"modelId": "claude-sonnet-4.6", "tokenLimits": {"maxInputTokens": 1000000, "maxOutputTokens": 64000}},
    {"modelId": "claude-haiku-4.5", "tokenLimits": {"maxInputTokens": 200000, "maxOutputTokens": 64000}},
    {"modelId": "claude-opus-4.5", "tokenLimits": {"maxInputTokens": 200000, "maxOutputTokens": 64000}},
    {"modelId": "claude-opus-4.6", "tokenLimits": {"maxInputTokens": 1000000, "maxOutputTokens": 64000}},
    {"modelId": "claude-opus-4.7", "tokenLimits": {"maxInputTokens": 666667, "maxOutputTokens": 128000}},
    {"modelId": "claude-opus-4.8", "tokenLimits": {"maxInputTokens": 666667, "maxOutputTokens": 128000}},
    {"modelId": "claude-opus-5", "tokenLimits": {"maxInputTokens": 666667, "maxOutputTokens": 128000}},
    {"modelId": "claude-sonnet-5", "tokenLimits": {"maxInputTokens": 666667, "maxOutputTokens": 64000}},
    {"modelId": "deepseek-3.2", "tokenLimits": {"maxInputTokens": 164000, "maxOutputTokens": 64000}},
    {"modelId": "glm-5", "tokenLimits": {"maxInputTokens": 200000, "maxOutputTokens": 64000}},
    {"modelId": "minimax-m2.1", "tokenLimits": {"maxInputTokens": 196000, "maxOutputTokens": 64000}},
    {"modelId": "minimax-m2.5", "tokenLimits": {"maxInputTokens": 196000, "maxOutputTokens": 64000}},
    {"modelId": "qwen3-coder-next", "tokenLimits": {"maxInputTokens": 256000, "maxOutputTokens": 64000}},
    {"modelId": "gpt-5.6-sol", "tokenLimits": {"maxInputTokens": 272000, "maxOutputTokens": 128000}},
    {"modelId": "gpt-5.6-terra", "tokenLimits": {"maxInputTokens": 272000, "maxOutputTokens": 128000}},
    {"modelId": "gpt-5.6-luna", "tokenLimits": {"maxInputTokens": 272000, "maxOutputTokens": 128000}},
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
# Endpoint Rotation
# ==================================================================================================

# Rotate to alternate generation endpoints when the primary one keeps failing.
# Disabled by default: only runtime.{region}.kiro.dev is verified for every
# credential type, and the alternates may reject some accounts. Enable it to get
# failover instead of a hard failure when the runtime host degrades.
KIRO_ENDPOINT_ROTATION: bool = os.getenv("KIRO_ENDPOINT_ROTATION", "false").lower() in ("true", "1", "yes")

# Comma-separated attempt order. Unknown keys are ignored.
# Available: runtime, codewhisperer, amazonq
KIRO_ENDPOINT_ORDER: list[str] = [
    part.strip() for part in os.getenv("KIRO_ENDPOINT_ORDER", "amazonq,codewhisperer,runtime").split(",") if part.strip()
]

# How long a failing endpoint is pushed to the back of the queue, in seconds.
# It is never removed: a cooldown must not turn a request into a hard failure.
KIRO_ENDPOINT_COOLDOWN_SECONDS: float = float(os.getenv("KIRO_ENDPOINT_COOLDOWN_SECONDS", "30"))

# Replace the generic sections of Anthropic's built-in Claude Code system prompt
# with a compact Kiro-identified preamble. The per-machine sections the client
# depends on - memory path, environment, language, skills - are preserved, as is
# anything the user supplied. Off by default: it changes agent behaviour.
CONDENSE_CLAUDE_PROMPT: bool = os.getenv("CONDENSE_CLAUDE_PROMPT", "false").lower() in ("true", "1", "yes")

# Task mode announced in conversationState, matching the official Kiro CLI.
# It sends "vibe" for free-form chat; "spec" and "task" are its other modes.
# Set to an empty value to omit the field entirely.
KIRO_AGENT_TASK_TYPE: str = os.getenv("KIRO_AGENT_TASK_TYPE", "vibe").strip()

# ==================================================================================================
# Debug Settings
# ==================================================================================================

# Debug logging mode:
# - off: disabled (default)
# - errors: save logs only for failed requests (4xx, 5xx)
# - all: save logs for every request (overwrites on each request)
_DEBUG_MODE_RAW: str = os.getenv("DEBUG_MODE", "").lower()
DEBUG_MODE: str

if _DEBUG_MODE_RAW in ("off", "errors", "all"):
    DEBUG_MODE = _DEBUG_MODE_RAW
else:
    DEBUG_MODE = "off"

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


DEBUG_CAPTURE_CONTENT: bool = os.getenv("DEBUG_CAPTURE_CONTENT", "false").lower() == "true"
DEBUG_CAPTURE_SUCCESS: bool = os.getenv("DEBUG_CAPTURE_SUCCESS", "false").lower() == "true"
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

# Payload token limit. Measured 2026-08-23 against runtime.us-east-1.kiro.dev
# / generateAssistantResponse / no tools:
#
#   claude-haiku-4.5 Hangul JSON  195,000 -> 200; 200,000 -> 400
#   claude-opus-5    Hangul JSON  800,000 -> 200; 1,000,000 -> 400
#                    CONTENT_LENGTH_EXCEEDS_THRESHOLD
#
# Default is the largest opus-5 size measured to pass. 1,000,000 Hangul is a
# size-reject. Do not apply the Claude CJK slope: 가 is 1 cl100k token.
KIRO_MAX_PAYLOAD_TOKENS: int = int(os.getenv("KIRO_MAX_PAYLOAD_TOKENS", "800000"))

# Legacy UTF-8 byte cap. Still honored when set so existing deployments do not
# silently widen. The 1,085,435 default was an ASCII-only bisect (1,085,435
# pass / 1,086,459 fail on an older host) and lets through ~250k-360k Hangul
# characters that the upstream then rejects with no numbers. Prefer
# KIRO_MAX_PAYLOAD_TOKENS. Set KIRO_MAX_PAYLOAD_BYTES=0 to disable this cap.
KIRO_MAX_PAYLOAD_BYTES: int = int(os.getenv("KIRO_MAX_PAYLOAD_BYTES", "1085435"))

# Auto-trim payload when over limit (default: false - disabled)
# Enable this if you use many tools (30+) and hit payload size errors
# When false, returns a clear error instead of trimming
AUTO_TRIM_PAYLOAD: bool = os.getenv("AUTO_TRIM_PAYLOAD", "false").lower() in ("true", "1", "yes")

# ==================================================================================================
# WebSearch Settings (MCP Tool Emulation)
# ==================================================================================================

# Enable web_search tool auto-injection (default: false)
# When enabled, web_search is automatically added as a tool for MCP emulation (Path B)
# Model decides whether to use it or not
#
# Off by default: injecting a tool the caller never asked for changes the shape
# of every request, and a client with its own search tool then sees two.
# Opt in with WEB_SEARCH_ENABLED=true.
#
# Note: Native Anthropic server-side tools (Path A) work ALWAYS, regardless of this setting
WEB_SEARCH_ENABLED: bool = os.getenv("WEB_SEARCH_ENABLED", "false").lower() in ("true", "1", "yes")

# ==================================================================================================
# Account System Settings
# ==================================================================================================

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

# Minimum quarantine in seconds for an account whose monthly quota is exhausted
# (402 MONTHLY_REQUEST_COUNT). Such an account cannot serve any request until
# its quota resets, so it leaves the rotation entirely: no probabilistic retry
# reaches it, unlike a Circuit Breaker cooldown. Persisted across restarts
# because the state outlives the process.
#
# This is the floor and the fallback, not the whole policy: when the usage poll
# knows the reset date the quarantine runs to that date instead. Waiting a fixed
# 6h was measured releasing accounts ~34-40h before their reset, which returned
# them to the pool reading "ready" at 1000/1000 - able only to answer 402 again.
# The floor still applies when the reset date is unknown or already past, so a
# bad reading cannot collapse the quarantine into an instant retry.
ACCOUNT_QUOTA_QUARANTINE: int = int(os.getenv("ACCOUNT_QUOTA_QUARANTINE", "21600"))

# Grace period added after the reported reset timestamp before an exhausted
# account is retried. The upstream boundary is a date, so retrying at the exact
# second risks spending another live request on a 402.
ACCOUNT_QUOTA_RESET_MARGIN: int = int(os.getenv("ACCOUNT_QUOTA_RESET_MARGIN", "300"))

# Ceiling on a reset-derived quarantine. A stale or malformed reset date must not
# translate into an effectively permanent exclusion; past this bound the account
# is retried and the upstream 402 becomes the authority again.
ACCOUNT_QUOTA_QUARANTINE_MAX: int = int(os.getenv("ACCOUNT_QUOTA_QUARANTINE_MAX", "2764800"))

# An upstream suspension is not a timed condition: the account stays locked until
# Kiro support restores it. The window only bounds how long the gateway trusts a
# stale verdict, so it is deliberately long - a suspended account must never draw
# traffic, and the probabilistic retry that rescues a merely broken account would
# only earn another 403 here.
ACCOUNT_SUSPENSION_QUARANTINE: int = int(os.getenv("ACCOUNT_SUSPENSION_QUARANTINE", "86400"))

# A refresh token the auth host has rejected is dead until a human re-logs in, so
# this window only bounds how long the gateway trusts that verdict without
# re-testing. It matches the suspension quarantine deliberately: both describe a
# condition no request can clear, and a shorter window would re-admit an account
# whose every request must fail at the token step, before a model is even chosen.
# Re-registering the account clears it immediately, which is the real remedy.
ACCOUNT_AUTH_DEAD_QUARANTINE: int = int(os.getenv("ACCOUNT_AUTH_DEAD_QUARANTINE", "86400"))

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

# Store the prompt and system prompt with each request log so the dashboard can
# show them. Off by default. Text is encrypted with LOG_ENCRYPTION_KEY.
CAPTURE_REQUEST_TEXT: bool = os.getenv("CAPTURE_REQUEST_TEXT", "false").lower() in ("true", "1", "yes")
CAPTURE_REQUEST_TEXT_MAX_CHARS: int = int(os.getenv("CAPTURE_REQUEST_TEXT_MAX_CHARS", "20000"))

# ==================================================================================================
# Account Cache Settings
# ==================================================================================================

# Model cache TTL in seconds (12 hours)
# Cache is refreshed only when account is used (not in background)
ACCOUNT_CACHE_TTL: int = int(os.getenv("ACCOUNT_CACHE_TTL", "43200"))

# ==================================================================================================
# State Persistence Settings
# ==================================================================================================

# Interval for periodic runtime-state saving in seconds
STATE_SAVE_INTERVAL_SECONDS: int = int(os.getenv("STATE_SAVE_INTERVAL_SECONDS", "10"))

# Dashboard live usage polling interval. A value of 0 disables background refresh;
# dashboard operators can still request a manual refresh.
USAGE_REFRESH_INTERVAL_SECONDS: int = int(os.getenv("USAGE_REFRESH_INTERVAL_SECONDS", "900"))

# ==================================================================================================
# Routing Policy
# ==================================================================================================

# Route by remaining monthly quota instead of pinning to the last account that
# succeeded. The global sticky index only moved on success, so a healthy account
# that was never reached stayed unreached: the pinned account answered every
# request and the pool drained one account at a time (observed live: one account
# took 11 of 11 requests while a 9%-used account took 0). Weighted selection
# spreads traffic so the pool exhausts together instead of serially.
ACCOUNT_QUOTA_WEIGHTED_ROUTING: bool = os.getenv("ACCOUNT_QUOTA_WEIGHTED_ROUTING", "true").lower() in (
    "true",
    "1",
    "yes",
)

# Weight floor for an account with no usable quota reading. Usage polling is
# control-plane telemetry that can lag a restart or fail per account, and a
# zero floor would make an unpolled account unreachable — reintroducing the
# starvation this policy exists to remove. Kept low so a known-idle account is
# still preferred over an unknown one.
ACCOUNT_UNKNOWN_QUOTA_WEIGHT: float = float(os.getenv("ACCOUNT_UNKNOWN_QUOTA_WEIGHT", "0.25"))

# Weight floor for an account whose quota is fully consumed. A 100%-used account
# is not necessarily refusing traffic (overage may be enabled, and the reading
# can be stale), so it stays reachable at a low weight rather than being pinned
# out of the pool by telemetry alone. Real exhaustion arrives as a 402 and is
# handled by quota_exhausted_until, which does remove the account.
ACCOUNT_DEPLETED_QUOTA_WEIGHT: float = float(os.getenv("ACCOUNT_DEPLETED_QUOTA_WEIGHT", "0.01"))

# Absolute floor under every routing weight, including an operator-configured
# one of 0. A zero weight cannot be sampled, so equally-zero accounts would be
# ordered by pool insertion and everything behind the first would never be
# reached - precisely the starvation weighted routing removes. Not configurable:
# it is the invariant that keeps the policy honest.
MINIMUM_ROUTING_WEIGHT: float = 1e-9

# ==================================================================================================
# Application Version
# ==================================================================================================

APP_VERSION: str = "0.1.0"
APP_TITLE: str = "kiro-lb"
APP_DESCRIPTION: str = (
    "Private Kiro API load balancer. OpenAI and Anthropic compatible; never fabricates reasoning content."
)


def get_kiro_refresh_url(region: str) -> str:
    """Return Kiro Desktop Auth token refresh URL for the specified region."""
    return KIRO_REFRESH_URL_TEMPLATE.format(region=region)


def get_aws_sso_oidc_url(region: str) -> str:
    """Return AWS SSO OIDC token URL for the specified region."""
    return AWS_SSO_OIDC_URL_TEMPLATE.format(region=region)


def get_kiro_api_host(region: str, is_builder_id: bool = False) -> str:
    """Return the runtime generation host used by current Kiro CLI clients."""
    return KIRO_API_HOST_TEMPLATE.format(region=region)


def get_kiro_q_host(region: str, is_builder_id: bool = False) -> str:
    """Return the Q API host for the region, routing Builder ID to Q Developer."""
    template = KIRO_BUILDER_ID_HOST_TEMPLATE if is_builder_id else KIRO_Q_HOST_TEMPLATE
    return template.format(region=region)
