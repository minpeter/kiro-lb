# -*- coding: utf-8 -*-
"""
Kiro Gateway - OpenAI-compatible interface for Kiro API.

Application entry point. Creates FastAPI app and connects routes.

Usage:
    # Using default settings (host: 0.0.0.0, port: 8000)
    python main.py

    # With CLI arguments (highest priority)
    python main.py --port 9000
    python main.py --host 127.0.0.1 --port 9000

    # With environment variables (medium priority)
    SERVER_PORT=9000 python main.py

    # Using uvicorn directly (uvicorn handles its own CLI args)
    uvicorn main:app --host 0.0.0.0 --port 8000

Priority: CLI args > Environment variables > Default values
"""

import argparse
import asyncio
import codecs
import json
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from kiro.account_manager import AccountManager
from kiro.config import (
    APP_DESCRIPTION,
    APP_TITLE,
    APP_VERSION,
    CAPTURE_REQUEST_TEXT_MAX_CHARS,
    DEFAULT_SERVER_HOST,
    DEFAULT_SERVER_PORT,
    LOG_LEVEL,
    PROXY_API_KEY,
    SERVER_HOST,
    SERVER_PORT,
    STREAMING_READ_TIMEOUT,
    USAGE_REFRESH_INTERVAL_SECONDS,
    VPN_PROXY_URL,
    _warn_timeout_configuration,
)
from kiro.dashboard import (
    flush_key_model_usage,
    initialize_dashboard_store,
    load_rate_observations,
    prune_rate_observations,
    prune_request_logs,
    record_rate_observations,
    record_request,
    refresh_all_account_usage,
)
from kiro.endpoint_settings import load_from_store as load_endpoint_settings
from kiro.agent_mode import load_from_store as load_agent_mode_setting
from kiro import proxy_chain
from kiro.gateway_tunables import CAPTURE_TEXT
from kiro.gateway_tunables import load_all as load_gateway_tunables
from kiro.log_crypto import ensure_key as ensure_log_key
from kiro.usage_tracking import current_request_credits
from kiro.prompt_filter import load_from_store as load_prompt_filter_setting
from kiro.dashboard import (
    router as dashboard_router,
)
from kiro.debug_middleware import DebugLoggerMiddleware
from kiro.exceptions import validation_exception_handler
from kiro.routes_anthropic import router as anthropic_router
from kiro.routes_openai import router as openai_router

# --- Loguru Configuration ---
logger.remove()
logger.add(
    sys.stderr,
    level=LOG_LEVEL,
    colorize=True,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
)


class InterceptHandler(logging.Handler):
    """
    Intercepts logs from standard logging and redirects them to loguru.

    This allows capturing logs from uvicorn, FastAPI and other libraries
    that use standard logging instead of loguru.

    Also filters out noisy shutdown-related exceptions (CancelledError, KeyboardInterrupt)
    that are normal during Ctrl+C but uvicorn logs as ERROR.
    """

    # Exceptions that are normal during shutdown and should not be logged as errors
    SHUTDOWN_EXCEPTIONS = (
        "CancelledError",
        "KeyboardInterrupt",
        "asyncio.exceptions.CancelledError",
    )

    def emit(self, record: logging.LogRecord) -> None:
        # Filter out shutdown-related exceptions that uvicorn logs as ERROR
        # These are normal during Ctrl+C and don't need to spam the console
        if record.exc_info:
            exc_type = record.exc_info[0]
            if exc_type is not None:
                exc_name = exc_type.__name__
                if exc_name in self.SHUTDOWN_EXCEPTIONS:
                    # Suppress the full traceback, just log a simple message
                    logger.info("Server shutdown in progress...")
                    return

        # Also filter by message content for cases where exc_info is not set
        msg = record.getMessage()
        if any(exc in msg for exc in self.SHUTDOWN_EXCEPTIONS):
            return

        # Get the corresponding loguru level
        level: str | int
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find the caller frame for correct source display
        frame = logging.currentframe()
        assert frame is not None
        depth = 2
        while frame.f_code.co_filename == logging.__file__:
            parent_frame = frame.f_back
            assert parent_frame is not None
            frame = parent_frame
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging_intercept():
    """
    Configures log interception from standard logging to loguru.

    Intercepts logs from:
    - uvicorn (access logs, error logs)
    - uvicorn.error
    - uvicorn.access
    - fastapi
    """
    # List of loggers to intercept
    loggers_to_intercept = [
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "fastapi",
    ]

    for logger_name in loggers_to_intercept:
        logging_logger = logging.getLogger(logger_name)
        logging_logger.handlers = [InterceptHandler()]
        logging_logger.propagate = False


# Configure uvicorn/fastapi log interception
setup_logging_intercept()


# ==================================================================================================
# VPN/Proxy Configuration
# ==================================================================================================
# Must be set BEFORE creating any httpx clients (including in lifespan)
# httpx automatically picks up HTTP_PROXY, HTTPS_PROXY, ALL_PROXY from environment

if VPN_PROXY_URL:
    # Normalize URL - add http:// if no scheme specified
    proxy_url_with_scheme = VPN_PROXY_URL if "://" in VPN_PROXY_URL else f"http://{VPN_PROXY_URL}"

    # Set environment variables for httpx to pick up automatically
    os.environ["HTTP_PROXY"] = proxy_url_with_scheme
    os.environ["HTTPS_PROXY"] = proxy_url_with_scheme
    os.environ["ALL_PROXY"] = proxy_url_with_scheme

    # Exclude localhost from proxy to avoid routing local requests through it
    no_proxy_hosts = os.environ.get("NO_PROXY", "")
    local_hosts = "127.0.0.1,localhost"
    if no_proxy_hosts:
        os.environ["NO_PROXY"] = f"{no_proxy_hosts},{local_hosts}"
    else:
        os.environ["NO_PROXY"] = local_hosts

    logger.info(f"Proxy configured: {proxy_url_with_scheme}")
    logger.debug(f"NO_PROXY: {os.environ['NO_PROXY']}")


# --- Configuration Validation ---
def validate_configuration() -> None:
    """Validate bootstrap configuration and open the private store.

    Accounts are managed via the dashboard (device login), not environment
    seed credentials. The process may start with an empty pool.
    """
    from kiro.store import initialize

    initialize()
    if not PROXY_API_KEY:
        logger.error("")
        logger.error("=" * 60)
        logger.error("  CONFIGURATION ERROR")
        logger.error("=" * 60)
        logger.error("  PROXY_API_KEY is required.")
        logger.error("  Generate one:  openssl rand -hex 32")
        logger.error('  Then set:      PROXY_API_KEY="..." in .env')
        logger.error("=" * 60)
        logger.error("")
        raise RuntimeError("Configuration validation failed")


# --- Lifespan Manager ---
class HandoffGateMiddleware:
    """Drain data-plane and account mutations during a blue/green handoff."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        app = scope.get("app")
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        guarded = path.startswith("/v1/") or (
            path.startswith("/api/dashboard/accounts") and method not in {"GET", "HEAD", "OPTIONS"}
        )
        if not guarded or app is None or not hasattr(app.state, "handoff_condition"):
            await self.app(scope, receive, send)
            return
        condition = app.state.handoff_condition
        async with condition:
            if app.state.handoff_quiesced:
                response = JSONResponse({"detail": "slot is quiesced for deployment"}, status_code=503)
                await response(scope, receive, send)
                return
            app.state.handoff_inflight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            async with condition:
                app.state.handoff_inflight -= 1
                condition.notify_all()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manages the application lifecycle.

    Creates and initializes:
    - Shared HTTP client with connection pooling
    - KiroAuthManager for token management
    - ModelInfoCache for model caching

    The shared HTTP client is used by all requests to reduce memory usage
    and enable connection reuse. This is especially important for handling
    concurrent requests efficiently (fixes issue #24).
    """
    logger.info("Starting application... Creating state managers.")
    app.state.started_at = time.time()
    initialize_dashboard_store()
    load_endpoint_settings()
    load_prompt_filter_setting()
    load_agent_mode_setting()
    load_gateway_tunables()
    if CAPTURE_TEXT.value():
        ensure_log_key()
    app.state.handoff_condition = asyncio.Condition()
    app.state.handoff_inflight = 0
    from kiro.store import can_write_runtime_state

    app.state.handoff_quiesced = not can_write_runtime_state()

    # Create shared HTTP client with connection pooling
    # This reduces memory usage and enables connection reuse across requests
    # Limits: max 100 total connections, max 20 keep-alive connections
    limits = httpx.Limits(
        max_connections=100,
        max_keepalive_connections=20,
        keepalive_expiry=30.0,  # Close idle connections after 30 seconds
    )
    # Timeout configuration for streaming (long read timeout for model "thinking")
    timeout = httpx.Timeout(
        connect=30.0,
        read=STREAMING_READ_TIMEOUT,  # 300 seconds for streaming
        write=30.0,
        pool=30.0,
    )
    app.state.http_client = httpx.AsyncClient(limits=limits, timeout=timeout, follow_redirects=True)
    logger.info("Shared HTTP client created with connection pooling")

    # Per-proxy clients mirror the shared client's limits and timeouts.
    proxy_chain.configure_clients(limits, timeout)
    proxy_chain.load_from_store()

    # ==============================================================================
    # Create AccountManager (accounts live in the private SQLite store)
    # ==============================================================================
    app.state.account_manager = AccountManager()

    # Load credentials and state
    await app.state.account_manager.load_credentials()
    await app.state.account_manager.load_state()

    # ==============================================================================
    # Initialize first working account (blocking)
    # ==============================================================================
    all_accounts = list(app.state.account_manager._accounts.keys())

    if not all_accounts:
        logger.warning("No accounts in the pool yet — open the dashboard and add one via device login")
    else:
        # Determine start index from persisted runtime state
        start_index = app.state.account_manager._current_account_index

        # Try to initialize accounts (full circle)
        initialized = False

        for i in range(len(all_accounts)):
            current_index = (start_index + i) % len(all_accounts)
            account_id = all_accounts[current_index]

            logger.info(f"Attempting to initialize account: {account_id}")

            success = await app.state.account_manager._initialize_account(account_id)

            if success:
                logger.info(f"Successfully initialized account: {account_id}")
                initialized = True
                break
            else:
                logger.warning(f"Failed to initialize account: {account_id}")

        if not initialized:
            logger.warning(
                "Failed to initialize any account; API will return errors until one works. "
                "Re-login via the dashboard if credentials are stale."
            )

    # Start background task for periodic state saving.
    save_task = asyncio.create_task(app.state.account_manager.save_state_periodically())

    async def refresh_usage_periodically() -> None:
        # Usage telemetry is control-plane observability. It must not delay
        # startup or impact the proxy data plane on endpoint/auth failures.
        while True:
            try:
                if not app.state.handoff_quiesced:
                    await refresh_all_account_usage(app.state.account_manager)
            except Exception as exc:
                logger.warning("Background Kiro usage refresh failed: {}", exc)
            await asyncio.sleep(max(USAGE_REFRESH_INTERVAL_SECONDS, 60))

    # Populate the dashboard promptly, then keep a bounded periodic cache.
    if not app.state.handoff_quiesced:
        try:
            await refresh_all_account_usage(app.state.account_manager)
        except Exception as exc:
            logger.warning("Initial Kiro usage refresh failed: {}", exc)
    usage_task = asyncio.create_task(refresh_usage_periodically())

    async def prune_request_logs_periodically() -> None:
        while True:
            removed = await asyncio.to_thread(prune_request_logs)
            if removed:
                logger.info("Pruned {} request-log row(s) past retention", removed)
            await asyncio.sleep(3600)

    prune_task = asyncio.create_task(prune_request_logs_periodically())

    # Restore the inferred rate limits. Without this every deploy resets the
    # estimate and the dashboard guide line disappears until a fresh 429.
    from kiro.config import RATE_ESTIMATE_WINDOW_SECONDS

    restored = await asyncio.to_thread(load_rate_observations, time.time() - RATE_ESTIMATE_WINDOW_SECONDS)
    if restored:
        app.state.account_manager.load_rate_observations(restored)
        logger.info("Restored {} rate observation(s) for limit inference", len(restored))

    async def flush_rate_observations() -> None:
        pending = app.state.account_manager.drain_unsaved_rate_observations()
        if pending and not await asyncio.to_thread(record_rate_observations, pending):
            app.state.account_manager.restore_unsaved_rate_observations(pending)

    async def persist_rate_observations_periodically() -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await flush_rate_observations()
                await asyncio.to_thread(prune_rate_observations)
                await asyncio.to_thread(flush_key_model_usage)
            except Exception as exc:
                logger.warning("Rate observation persistence failed: {}", exc)

    rate_task = asyncio.create_task(persist_rate_observations_periodically())

    logger.info("Account system initialized successfully")

    yield

    # Graceful shutdown
    logger.info("Shutting down application...")

    # Cancel background tasks.
    for task in (save_task, usage_task, prune_task, rate_task):
        task.cancel()

    # Keep the rate estimate and per-key usage across restarts.
    try:
        await flush_rate_observations()
        await asyncio.to_thread(flush_key_model_usage)
    except Exception as exc:
        logger.warning("Final rate observation flush failed: {}", exc)
    for task in (save_task, usage_task):
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Final state save
    await app.state.account_manager._save_state()
    logger.info("Final state saved")

    # Close HTTP client
    try:
        await app.state.http_client.aclose()
        logger.info("Shared HTTP client closed")
    except Exception as e:
        logger.warning(f"Error closing shared HTTP client: {e}")

    try:
        await proxy_chain.close_clients()
    except Exception as e:
        logger.warning(f"Error closing proxy clients: {e}")


# --- FastAPI Application ---
app = FastAPI(title=APP_TITLE, description=APP_DESCRIPTION, version=APP_VERSION, lifespan=lifespan)
app.add_middleware(HandoffGateMiddleware)


def _authorize_handoff(request: Request, secret: str | None) -> None:
    expected = os.getenv("HANDOFF_SECRET", "")
    host = request.headers.get("host", "").split(":", 1)[0]
    if not expected or host not in {"127.0.0.1", "localhost", "::1"} or secret != expected:
        raise HTTPException(status_code=403, detail="handoff control is direct-slot only")


@app.post("/_internal/handoff/quiesce", include_in_schema=False)
async def handoff_quiesce(request: Request, x_handoff_secret: str | None = Header(default=None)):
    """Stop new work, drain existing streams, and persist the writer snapshot."""
    _authorize_handoff(request, x_handoff_secret)
    condition = request.app.state.handoff_condition
    async with condition:
        request.app.state.handoff_quiesced = True
        await condition.wait_for(lambda: request.app.state.handoff_inflight == 0)
    await request.app.state.account_manager.flush_for_handoff()
    return {"ready": True, "state": "quiesced"}


@app.post("/_internal/handoff/activate", include_in_schema=False)
async def handoff_activate(request: Request, x_handoff_secret: str | None = Header(default=None)):
    """Reload the final writer snapshot and permit this slot to serve traffic."""
    _authorize_handoff(request, x_handoff_secret)
    from kiro.store import can_write_runtime_state

    if not can_write_runtime_state():
        raise HTTPException(status_code=409, detail="slot does not own runtime state")
    await request.app.state.account_manager.reload_durable_state()
    request.app.state.handoff_quiesced = False
    return {"ready": True, "state": "active"}


@app.get("/_internal/handoff/ready", include_in_schema=False)
async def handoff_ready(request: Request, x_handoff_secret: str | None = Header(default=None)):
    """Authenticated direct-slot readiness used by the deploy script."""
    _authorize_handoff(request, x_handoff_secret)
    ready = not request.app.state.handoff_quiesced and bool(request.app.state.account_manager._accounts)
    if not ready:
        raise HTTPException(status_code=503, detail="slot is not active")
    return {"ready": True, "state": "active"}


# --- CORS Middleware ---
# Allow CORS for all origins to support browser clients
# and tools that send preflight OPTIONS requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allow all methods (GET, POST, OPTIONS, etc.)
    allow_headers=["*"],  # Allow all headers
)


# --- Debug Logger Middleware ---
# Initializes debug logging BEFORE Pydantic validation
# This allows capturing validation errors (422) in debug logs
app.add_middleware(DebugLoggerMiddleware)


# --- Validation Error Handler Registration ---
app.add_exception_handler(
    RequestValidationError,
    validation_exception_handler,  # type: ignore[arg-type]  # Starlette types handlers invariantly as Exception.
)


# --- Route Registration ---
# OpenAI-compatible API: /v1/models, /v1/chat/completions
app.include_router(openai_router)

# Anthropic-compatible API: /v1/messages
app.include_router(anthropic_router)

# Private operations dashboard and metadata-only request log API.
app.include_router(dashboard_router)
_static_dir = Path(__file__).parent / "kiro" / "static"
app.mount("/assets", StaticFiles(directory=_static_dir / "assets"), name="dashboard-assets")
app.mount("/fonts", StaticFiles(directory=_static_dir / "fonts"), name="dashboard-fonts")


@app.get("/kiro-icon.svg", include_in_schema=False)
async def dashboard_icon():
    """Serve the vendored Kiro brand icon used by the dashboard shell."""
    from fastapi.responses import FileResponse

    return FileResponse(_static_dir / "kiro-icon.svg", media_type="image/svg+xml")


@app.get("/favicon.svg", include_in_schema=False)
async def dashboard_favicon():
    from fastapi.responses import FileResponse

    return FileResponse(_static_dir / "kiro-icon.svg", media_type="image/svg+xml")


@app.middleware("http")
async def dashboard_request_metrics(request, call_next):
    """Record /v1 request metadata, plus prompt text when capture is enabled."""
    started = time.perf_counter()
    model = None
    prompt = None
    system_prompt = None
    is_data_plane = request.url.path.startswith("/v1/")
    capture = is_data_plane and CAPTURE_TEXT.value()

    if is_data_plane and request.headers.get("content-type", "").startswith("application/json"):
        try:
            payload = json.loads((await request.body()).decode("utf-8"))
            if isinstance(payload, dict):
                model = payload.get("model")
                if capture:
                    prompt = _last_user_text(payload.get("messages"))
                    system_prompt = _system_text(payload.get("system"))
        except Exception:
            pass

    if not is_data_plane:
        return await call_next(request)

    # Seeded before the request runs so the streaming layer, which executes in
    # its own task, mutates this same dict.
    credit_holder: dict = {}
    current_request_credits.set(credit_holder)

    response = None
    collected: list[str] = []
    try:
        response = await call_next(request)
        if capture and hasattr(response, "body_iterator"):
            response = _tee_response(response, collected)
        return response
    finally:
        # Streaming responses finish after this returns, so the recording is
        # deferred to the tee's completion; a non-streamed one records here.
        if response is None or not (capture and hasattr(response, "body_iterator")):
            _persist(request, model, response, started, prompt, system_prompt, None, credit_holder)
        else:
            _pending.append((request, model, response, started, prompt, system_prompt, collected, credit_holder))


# Streamed requests are recorded when their body finishes, not when the
# middleware returns. One entry per in-flight streamed response.
_pending: list = []


_USAGE_INPUT = re.compile(r'"input_tokens"\s*:\s*(\d+)')
_USAGE_OUTPUT = re.compile(r'"output_tokens"\s*:\s*(\d+)')
_USAGE_PROMPT = re.compile(r'"prompt_tokens"\s*:\s*(\d+)')
_USAGE_COMPLETION = re.compile(r'"completion_tokens"\s*:\s*(\d+)')
# Kiro reports what a request actually cost; the field name varies by stream
# version. Nothing is derived from tokens: Kiro publishes no token-to-credit
# rate, so a computed figure would be invented.
_CREDITS = re.compile(r'"(?:creditUsage|credit_usage|creditsConsumed)"\s*:\s*([0-9]*\.?[0-9]+)')


def _usage_from_response(raw: str) -> tuple[int | None, int | None, float | None]:
    """Pull token counts and reported credits out of a captured response."""
    if not raw:
        return None, None, None

    def last_int(pattern):
        found = pattern.findall(raw)
        return int(found[-1]) if found else None

    # message_start carries the input count, message_delta the running output,
    # so the last occurrence of each is the final figure.
    input_tokens = last_int(_USAGE_INPUT) or last_int(_USAGE_PROMPT)
    output_tokens = last_int(_USAGE_OUTPUT) or last_int(_USAGE_COMPLETION)

    credits = None
    reported = _CREDITS.findall(raw)
    if reported:
        try:
            credits = sum(float(value) for value in reported)
        except ValueError:
            credits = None
    return input_tokens, output_tokens, credits


def _persist(request, model, response, started, prompt, system_prompt, collected, credit_holder=None):
    status = getattr(response, "status_code", None) or 500
    raw_response = "".join(collected) if collected else ""
    input_tokens, output_tokens, credits = _usage_from_response(raw_response)
    # What Kiro reported wins: the client-facing stream does not carry it.
    reported = (credit_holder or {}).get("credits")
    if reported:
        credits = reported
    record_request(
        request.url.path,
        model,
        status,
        int((time.perf_counter() - started) * 1000),
        client_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        prompt=prompt,
        system_prompt=system_prompt,
        response_text=_readable_response(raw_response) if raw_response else None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        credits_spent=credits,
    )


def _tee_response(response, collected: list[str]):
    """Pass every chunk through untouched while keeping a bounded copy.

    Buffering the whole body would defeat streaming and could hold a large
    response in memory, so the copy stops at the configured cap.
    """
    original = response.body_iterator
    cap = CAPTURE_REQUEST_TEXT_MAX_CHARS
    # An emoji can straddle a chunk boundary, so decoding must carry state;
    # decoding each chunk on its own corrupts whatever spans the split.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    kept = 0

    async def relay():
        nonlocal kept
        try:
            async for chunk in original:
                if kept < cap:
                    raw = chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")
                    text = decoder.decode(raw)
                    if text:
                        collected.append(text[: cap - kept])
                        kept += len(text)
                yield chunk
        finally:
            entry = next((item for item in _pending if item[2] is response), None)
            if entry is not None:
                _pending.remove(entry)
                _persist(*entry)

    response.body_iterator = relay()
    return response


_SSE_TEXT = re.compile(r'"(?:text|content|text_delta)"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _readable_response(raw: str) -> str:
    """Turn an SSE stream into the text the client would have shown.

    A raw event stream is mostly framing, so the deltas are joined when the
    body looks like SSE. Each delta is unescaped with the JSON decoder: a
    unicode_escape round-trip mangles anything outside latin-1, which turned
    emoji and accented characters into mojibake.
    """
    if "data:" not in raw:
        return raw
    parts: list[str] = []
    for match in _SSE_TEXT.finditer(raw):
        try:
            parts.append(json.loads(f'"{match.group(1)}"'))
        except ValueError:
            parts.append(match.group(1))
    return "".join(parts) if parts else raw


def _block_text(content) -> str:
    """Flatten a string or a list of content blocks into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [block.get("text", "") for block in content if isinstance(block, dict) and block.get("text")]
        return "\n".join(parts)
    return ""


def _last_user_text(messages) -> str | None:
    """The most recent user message, which is what the request is asking."""
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            text = _block_text(message.get("content"))
            if text:
                return text
    return None


def _system_text(system) -> str | None:
    text = _block_text(system)
    return text or None


# --- Uvicorn log config ---
# Minimal configuration for redirecting uvicorn logs to loguru.
# Uses InterceptHandler which intercepts logs and passes them to loguru.
UVICORN_LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "default": {
            "class": "main.InterceptHandler",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.access": {"handlers": ["default"], "level": "INFO", "propagate": False},
    },
}


def parse_cli_args() -> argparse.Namespace:
    """
    Parse command-line arguments for server configuration.

    CLI arguments have the highest priority, overriding both
    environment variables and default values.

    Returns:
        Parsed arguments namespace with host and port values
    """
    parser = argparse.ArgumentParser(
        description=f"{APP_TITLE} - {APP_DESCRIPTION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Configuration Priority (highest to lowest):
  1. CLI arguments (--host, --port)
  2. Environment variables (SERVER_HOST, SERVER_PORT)
  3. Default values (0.0.0.0:8000)

Examples:
  python main.py                          # Use defaults or env vars
  python main.py --port 9000              # Override port only
  python main.py --host 127.0.0.1         # Local connections only
  python main.py -H 0.0.0.0 -p 8080       # Short form

  SERVER_PORT=9000 python main.py         # Via environment
  uvicorn main:app --port 9000            # Via uvicorn directly
        """,
    )

    parser.add_argument(
        "-H",
        "--host",
        type=str,
        default=None,  # None means "use env or default"
        metavar="HOST",
        help=f"Server host address (default: {DEFAULT_SERVER_HOST}, env: SERVER_HOST)",
    )

    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=None,  # None means "use env or default"
        metavar="PORT",
        help=f"Server port (default: {DEFAULT_SERVER_PORT}, env: SERVER_PORT)",
    )

    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {APP_VERSION}")

    return parser.parse_args()


def resolve_server_config(args: argparse.Namespace) -> tuple[str, int]:
    """
    Resolve final server configuration using priority hierarchy.

    Priority (highest to lowest):
    1. CLI arguments (--host, --port)
    2. Environment variables (SERVER_HOST, SERVER_PORT)
    3. Default values (0.0.0.0:8000)

    Args:
        args: Parsed CLI arguments

    Returns:
        Tuple of (host, port) with resolved values
    """
    # Host resolution: CLI > ENV > Default
    if args.host is not None:
        final_host = args.host
        host_source = "CLI argument"
    elif SERVER_HOST != DEFAULT_SERVER_HOST:
        final_host = SERVER_HOST
        host_source = "environment variable"
    else:
        final_host = DEFAULT_SERVER_HOST
        host_source = "default"

    # Port resolution: CLI > ENV > Default
    if args.port is not None:
        final_port = args.port
        port_source = "CLI argument"
    elif SERVER_PORT != DEFAULT_SERVER_PORT:
        final_port = SERVER_PORT
        port_source = "environment variable"
    else:
        final_port = DEFAULT_SERVER_PORT
        port_source = "default"

    # Log configuration sources for transparency
    logger.debug(f"Host: {final_host} (from {host_source})")
    logger.debug(f"Port: {final_port} (from {port_source})")

    return final_host, final_port


def print_startup_banner(host: str, port: int) -> None:
    """
    Print a startup banner with server information.

    Args:
        host: Server host address
        port: Server port
    """
    # ANSI color codes
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    WHITE = "\033[97m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"

    # Determine display URL
    display_host = "localhost" if host == "0.0.0.0" else host
    url = f"http://{display_host}:{port}"

    print()
    print(f"  {WHITE}{BOLD}👻 {APP_TITLE} v{APP_VERSION}{RESET}")
    print()
    print(f"  {WHITE}Server running at:{RESET}")
    print(f"  {GREEN}{BOLD}➜  {url}{RESET}")
    print()
    print(f"  {DIM}API Docs:      {url}/docs{RESET}")
    print(f"  {DIM}Health Check:  {url}/health{RESET}")
    print()
    print(f"  {DIM}{'─' * 48}{RESET}")
    print(f"  {WHITE}💬 Found a bug? Need help? Have questions?{RESET}")
    print(f"  {YELLOW}➜  https://github.com/minpeter/kiro-lb-python/issues{RESET}")
    print(f"  {DIM}{'─' * 48}{RESET}")
    print()


# --- Entry Point ---
if __name__ == "__main__":
    import uvicorn

    # Parse CLI arguments first (handles --version, --help without requiring config)
    args = parse_cli_args()

    # Run configuration validation before starting server
    validate_configuration()

    # Warn about suboptimal timeout configuration
    _warn_timeout_configuration()

    # Resolve final configuration with priority hierarchy
    final_host, final_port = resolve_server_config(args)

    # Print startup banner
    print_startup_banner(final_host, final_port)

    logger.info(f"Starting Uvicorn server on {final_host}:{final_port}...")

    # Use string reference to avoid double module import
    uvicorn.run(
        "main:app",
        host=final_host,
        port=final_port,
        log_config=UVICORN_LOG_CONFIG,
    )
