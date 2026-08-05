# -*- coding: utf-8 -*-
"""
FastAPI routes for Kiro Gateway.

Contains all API endpoints:
- / and /health: Health check
- /v1/models: Models list
- /v1/chat/completions: Chat completions
"""

import json
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from loguru import logger

from kiro.auth import AuthType
from kiro.config import (
    APP_VERSION,
    PROFILE_ARN,
    WEB_SEARCH_ENABLED,
)
from kiro.converters_openai import build_kiro_payload
from kiro.dashboard import identify_data_api_key
from kiro.http_client import KiroHttpClient
from kiro.models_openai import (
    ChatCompletionRequest,
    ModelList,
    OpenAIModel,
)
from kiro.payload_guards import PayloadTooLargeError
from kiro.streaming_openai import collect_stream_response, stream_with_first_token_retry
from kiro.usage_tracking import current_account_id, current_api_key_id
from kiro.utils import generate_conversation_id

if TYPE_CHECKING:
    from kiro.debug_logger import DebugLogger

# Import debug_logger
debug_logger: Optional["DebugLogger"]
try:
    import kiro.debug_logger as debug_logger_module
except ImportError:
    debug_logger = None
else:
    debug_logger = debug_logger_module.debug_logger


# --- Security scheme ---
api_key_header = APIKeyHeader(name="Authorization", auto_error=False)


async def verify_api_key(auth_header: str = Security(api_key_header)) -> bool:
    """
    Verify API key in Authorization header.

    Expects format: "Bearer {PROXY_API_KEY}"

    Args:
        auth_header: Authorization header value

    Returns:
        True if key is valid

    Raises:
        HTTPException: 401 if key is invalid or missing
    """
    if not auth_header or not auth_header.startswith("Bearer "):
        logger.warning("Access attempt with missing API key.")
        raise HTTPException(status_code=401, detail="Invalid or missing API Key")
    key_id = identify_data_api_key(auth_header.removeprefix("Bearer "))
    if key_id is None:
        logger.warning("Access attempt with invalid API key.")
        raise HTTPException(status_code=401, detail="Invalid or missing API Key")
    current_api_key_id.set(key_id)
    return True


# Model discovery is the one data-plane route both client families hit with their
# own auth style: Claude Code sends x-api-key, OpenAI clients send Bearer. The
# chat routes keep their protocol-specific verifiers.
anthropic_api_key_header = APIKeyHeader(name="x-api-key", auto_error=False)


async def verify_models_api_key(
    auth_header: str = Security(api_key_header),
    x_api_key: str = Security(anthropic_api_key_header),
) -> bool:
    """
    Verify the API key on /v1/models, accepting Bearer or x-api-key.

    Args:
        auth_header: Authorization header value
        x_api_key: x-api-key header value used by Anthropic clients

    Returns:
        True if key is valid

    Raises:
        HTTPException: 401 if key is invalid or missing
    """
    if auth_header and auth_header.startswith("Bearer "):
        raw_key = auth_header.removeprefix("Bearer ")
    elif x_api_key:
        raw_key = x_api_key
    else:
        logger.warning("Access attempt with missing API key.")
        raise HTTPException(status_code=401, detail="Invalid or missing API Key")

    key_id = identify_data_api_key(raw_key)
    if key_id is None:
        logger.warning("Access attempt with invalid API key.")
        raise HTTPException(status_code=401, detail="Invalid or missing API Key")
    current_api_key_id.set(key_id)
    return True


# --- Router ---
router = APIRouter()


@router.get("/healthz")
async def root():
    """
    Health check endpoint.

    Returns:
        Status and application version
    """
    return {"status": "ok", "message": "kiro-lb is running", "version": APP_VERSION}


@router.get("/health")
async def health():
    """
    Detailed health check.

    Returns:
        Status, timestamp and version
    """
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat(), "version": APP_VERSION}


# Model ID prefix -> the vendor that actually owns it. Kiro fronts several
# vendors, so a blanket owned_by="anthropic" mislabels most of the catalog.
_MODEL_OWNERS = (
    ("claude", "anthropic"),
    ("gpt-", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("deepseek", "deepseek"),
    ("qwen", "alibaba"),
    ("minimax", "minimax"),
    ("glm", "zhipu"),
)


def _model_owner(model_id: str) -> str:
    """Resolve the owning vendor from the model id, defaulting to the gateway."""
    normalized = model_id.strip().lower()
    for prefix, owner in _MODEL_OWNERS:
        if normalized.startswith(prefix):
            return owner
    return "kiro"


def _display_name(model_id: str) -> str:
    """Human label for the Anthropic display_name field."""
    return f"{model_id} (Kiro)"


def _resolve_model_limits(request: Request, model_ids: List[str]) -> Dict[str, Tuple[Optional[int], Optional[int]]]:
    """
    Look up (maxInputTokens, maxOutputTokens) per model from the account caches.

    The gateway already resolves these limits for payload sizing, so the endpoint
    reuses them rather than making clients hardcode a window. Entries missing
    from every cache stay None instead of being guessed.
    """
    caches = []
    account_manager = request.app.state.account_manager
    for account in account_manager._accounts.values():
        cache = getattr(account, "model_cache", None)
        if cache is not None:
            caches.append(cache)

    limits: Dict[str, Tuple[Optional[int], Optional[int]]] = {}
    for model_id in model_ids:
        for cache in caches:
            info = cache.get(model_id)
            if not info:
                continue
            token_limits = info.get("tokenLimits") or {}
            limits[model_id] = (token_limits.get("maxInputTokens"), token_limits.get("maxOutputTokens"))
            break
    return limits


@router.get("/v1/models", response_model=ModelList, dependencies=[Depends(verify_models_api_key)])
async def get_models(request: Request):
    """
    Return list of available models.

    Serves the OpenAI and Anthropic schemas as one superset response, because both
    routers mount on the same app and a second /v1/models registration would be
    shadowed. Claude Code 2.1.126+ reads the Anthropic fields for gateway model
    discovery; OpenAI clients read theirs and ignore the rest.

    Args:
        request: FastAPI Request for accessing app.state

    Returns:
        ModelList with available models in consistent format (with dots)
    """
    logger.info("Request to /v1/models")

    # Get available models based on mode
    if request.app.state.account_system:
        # Account system: collect models from all initialized accounts
        available_model_ids = request.app.state.account_manager.get_all_available_models()
    else:
        # Legacy: use resolver from first account
        account = request.app.state.account_manager.get_first_account()
        available_model_ids = account.model_resolver.get_available_models()

    limits = _resolve_model_limits(request, available_model_ids)
    created = int(time.time())
    created_at = datetime.fromtimestamp(created, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    openai_models = []
    for model_id in available_model_ids:
        owner = _model_owner(model_id)
        max_input, max_output = limits.get(model_id, (None, None))
        openai_models.append(
            OpenAIModel(
                id=model_id,
                owned_by=owner,
                description=f"{model_id} via Kiro API",
                display_name=_display_name(model_id),
                created=created,
                created_at=created_at,
                context_window=max_input,
                max_input_tokens=max_input,
                max_tokens=max_output,
            )
        )

    return ModelList(
        data=openai_models,
        first_id=openai_models[0].id if openai_models else None,
        last_id=openai_models[-1].id if openai_models else None,
    )


@router.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
async def chat_completions(request: Request, request_data: ChatCompletionRequest):
    """
    Chat completions endpoint - compatible with OpenAI API.

    Accepts requests in OpenAI format and translates them to Kiro API.
    Supports streaming and non-streaming modes.

    Args:
        request: FastAPI Request for accessing app.state
        request_data: Request in OpenAI ChatCompletionRequest format

    Returns:
        StreamingResponse for streaming mode
        JSONResponse for non-streaming mode

    Raises:
        HTTPException: On validation or API errors
    """
    logger.info(f"Request to /v1/chat/completions (model={request_data.model}, stream={request_data.stream})")

    # Note: prepare_new_request() and log_request_body() are now called by DebugLoggerMiddleware
    # This ensures debug logging works even for requests that fail Pydantic validation (422 errors)

    # ==============================================================================
    # WebSearch Support - Path B: Auto-Injection (MCP Tool Emulation)
    # ==============================================================================

    # Auto-inject web_search tool if enabled (Path B - MCP emulation)
    if WEB_SEARCH_ENABLED:
        if request_data.tools is None:
            request_data.tools = []

        # Check if web_search already exists
        has_ws = any(
            getattr(tool, "type", None) == "function"
            and getattr(getattr(tool, "function", None), "name", None) == "web_search"
            for tool in request_data.tools
        )

        if not has_ws:
            from kiro.models_openai import Tool, ToolFunction

            web_search_tool = Tool(
                type="function",
                function=ToolFunction(
                    name="web_search",
                    description="Search the web for current information. Use when you need up-to-date data from the internet.",
                    parameters={
                        "type": "object",
                        "properties": {"query": {"type": "string", "description": "Search query"}},
                        "required": ["query"],
                    },
                ),
            )
            request_data.tools.append(web_search_tool)
            logger.debug("Auto-injected web_search tool for MCP emulation (Path B)")

    # ==============================================================================
    # Account System: Account System Failover or Legacy Mode
    # ==============================================================================

    account_system = request.app.state.account_system
    while True:
        from kiro.account_errors import ErrorType, classify_error

        account_manager = request.app.state.account_manager
        all_accounts = list(account_manager._accounts.keys())
        single_attempt = not account_system or len(all_accounts) == 1
        max_attempts = 1 if not account_system else max(1, len(all_accounts) * 2)

        last_error_message = None
        last_error_status = None
        tried_accounts: set[str] = set()  # Track tried accounts in current failover loop

        for _attempt in range(max_attempts):
            account = (
                await account_manager.get_next_account(request_data.model, exclude_accounts=tried_accounts)
                if account_system
                else account_manager.get_first_account()
            )

            if account is None or not account.auth_manager:
                # All accounts unavailable
                if single_attempt:
                    # Single account - return original error with original status code
                    if not account_system:
                        logger.error("No initialized accounts available (legacy mode)")
                        raise HTTPException(503, "No initialized accounts available")
                    raise HTTPException(
                        status_code=last_error_status or 503, detail=last_error_message or "Account unavailable"
                    )
                else:
                    # Multiple accounts - no account is currently selectable.
                    # Report the pool state so the operator can tell a rate-limit
                    # burst apart from cooldowns or auth failures.
                    detail = (
                        "No available accounts for this model. "
                        f"Pool state: {account_manager.describe_pool_state(tried_accounts)}."
                    )
                    if last_error_message:
                        detail += f" Error from last account: {last_error_message}"
                    raise HTTPException(status_code=503, detail=detail)

            # Mark account as tried in current failover loop
            tried_accounts.add(account.id)
            # Attribute any tokens this attempt produces to this account. Set per
            # attempt, not once per request: on failover the tokens belong to the
            # account that actually answered, and only the last write survives.
            current_account_id.set(account.id)

            # Use objects from account
            auth_manager = account.auth_manager
            model_cache = account.model_cache

            # Generate conversation ID
            conversation_id = generate_conversation_id()

            # Build payload for Kiro
            # A Builder ID account has no profile and must not be given one: the
            # global fallback would send a foreign ARN and fail the request.
            profile_arn_for_payload = auth_manager.profile_arn or (
                "" if auth_manager.auth_type == AuthType.AWS_SSO_OIDC else PROFILE_ARN or ""
            )

            try:
                kiro_payload = build_kiro_payload(request_data, conversation_id, profile_arn_for_payload)
            except PayloadTooLargeError as e:
                raise HTTPException(status_code=400, detail=str(e))
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

            # Log Kiro payload
            try:
                kiro_request_body = json.dumps(kiro_payload, ensure_ascii=False, indent=2).encode("utf-8")
                if debug_logger:
                    debug_logger.log_kiro_request_body(kiro_request_body)
            except Exception as e:
                logger.warning(f"Failed to log Kiro request: {e}")

            # Create HTTP client
            url = f"{auth_manager.api_host}/generateAssistantResponse"
            logger.debug(f"Kiro API URL: {url} (account: {account.id})")

            if request_data.stream:
                http_client = KiroHttpClient(auth_manager, shared_client=None)
            else:
                shared_client = request.app.state.http_client
                http_client = KiroHttpClient(auth_manager, shared_client=shared_client)

            try:
                # Make request to Kiro API
                response = await http_client.request_with_retry(
                    "POST", url, kiro_payload, stream=True, retry_rate_limits=False
                )

                if response.status_code == 200:
                    # Prepare data for token counting
                    messages_for_tokenizer = [msg.model_dump() for msg in request_data.messages]
                    tools_for_tokenizer = (
                        [tool.model_dump() for tool in request_data.tools] if request_data.tools else None
                    )

                    if request_data.stream:
                        # Streaming mode
                        async def stream_wrapper():
                            streaming_error = None
                            client_disconnected = False
                            try:

                                async def make_retry_request():
                                    return await http_client.request_with_retry(
                                        "POST", url, kiro_payload, stream=True, retry_rate_limits=False
                                    )

                                async for chunk in stream_with_first_token_retry(
                                    make_request=make_retry_request,
                                    client=http_client.client,
                                    model=request_data.model,
                                    model_cache=model_cache,
                                    auth_manager=auth_manager,
                                    initial_response=response,
                                    request_messages=messages_for_tokenizer,
                                    request_tools=tools_for_tokenizer,
                                    include_reasoning=request_data.include_reasoning,
                                    parallel_tool_calls=request_data.parallel_tool_calls is not False,
                                ):
                                    yield chunk
                                if account_system:
                                    await account_manager.report_success(account.id, request_data.model)
                            except GeneratorExit:
                                client_disconnected = True
                                logger.debug("Client disconnected during streaming (GeneratorExit in routes)")
                            except Exception as e:
                                streaming_error = e
                                raise
                            finally:
                                await http_client.close()
                                if streaming_error:
                                    error_type = type(streaming_error).__name__
                                    error_msg = str(streaming_error) if str(streaming_error) else "(empty message)"
                                    logger.error(
                                        f"HTTP 500 - POST /v1/chat/completions (streaming) - [{error_type}] {error_msg[:100]}"
                                    )
                                elif client_disconnected:
                                    logger.info(
                                        "HTTP 200 - POST /v1/chat/completions (streaming) - client disconnected"
                                    )
                                else:
                                    logger.info("HTTP 200 - POST /v1/chat/completions (streaming) - completed")
                                if debug_logger:
                                    if streaming_error:
                                        debug_logger.flush_on_error(500, str(streaming_error))
                                    else:
                                        debug_logger.discard_buffers()

                        return StreamingResponse(stream_wrapper(), media_type="text/event-stream")

                    else:
                        # Non-streaming mode
                        client = http_client.client
                        assert client is not None
                        openai_response = await collect_stream_response(
                            client,
                            response,
                            request_data.model,
                            model_cache,
                            auth_manager,
                            request_messages=messages_for_tokenizer,
                            request_tools=tools_for_tokenizer,
                            include_reasoning=request_data.include_reasoning,
                            parallel_tool_calls=request_data.parallel_tool_calls is not False,
                        )
                        if account_system:
                            await account_manager.report_success(account.id, request_data.model)

                        await http_client.close()
                        logger.info("HTTP 200 - POST /v1/chat/completions (non-streaming) - completed")

                        if debug_logger:
                            debug_logger.discard_buffers()

                        return JSONResponse(content=openai_response)

                else:
                    # ERROR - classify and decide
                    try:
                        error_content = await response.aread()
                    except Exception:
                        error_content = b"Unknown error"

                    await http_client.close()
                    error_text = error_content.decode("utf-8", errors="replace")

                    # Extract error reason and save for final return
                    error_reason = None
                    # The upstream message is kept verbatim: on the legacy q.*
                    # host a suspension arrives with reason=null and the verdict
                    # only in the message, so report_failure needs the raw text
                    # to quarantine the account instead of retrying it forever.
                    upstream_message = error_text
                    try:
                        error_json = json.loads(error_text)
                        from kiro.kiro_errors import enhance_kiro_error

                        error_info = enhance_kiro_error(error_json)
                        error_reason = error_info.reason
                        upstream_message = error_info.original_message or error_text
                        last_error_message = error_info.user_message
                        last_error_status = response.status_code
                        logger.debug(
                            f"Original Kiro error: {error_info.original_message} (reason: {error_info.reason})"
                        )
                    except (json.JSONDecodeError, KeyError):
                        last_error_message = error_text
                        last_error_status = response.status_code

                    # Classify error
                    error_type = classify_error(response.status_code, error_reason)

                    if error_type == ErrorType.FATAL:
                        # FATAL - return to client immediately
                        if account_system:
                            await account_manager.report_failure(
                                account.id,
                                request_data.model,
                                error_type,
                                response.status_code,
                                error_reason,
                                upstream_message,
                            )

                        logger.warning(
                            f"HTTP {response.status_code} - POST /v1/chat/completions - {last_error_message[:100]}"
                        )

                        if debug_logger:
                            debug_logger.flush_on_error(response.status_code, last_error_message)

                        return JSONResponse(
                            status_code=response.status_code,
                            content={
                                "error": {
                                    "message": last_error_message,
                                    "type": "kiro_api_error",
                                    "code": response.status_code,
                                }
                            },
                        )

                    else:  # ErrorType.RECOVERABLE
                        # RECOVERABLE - try next account
                        if account_system:
                            await account_manager.report_failure(
                                account.id,
                                request_data.model,
                                error_type,
                                response.status_code,
                                error_reason,
                                upstream_message,
                            )

                        if single_attempt:
                            if not account_system:
                                return JSONResponse(
                                    status_code=response.status_code,
                                    content={
                                        "error": {
                                            "message": last_error_message,
                                            "type": "kiro_api_error",
                                            "code": response.status_code,
                                        }
                                    },
                                )
                            break

                        continue  # Next iteration

            except HTTPException as e:
                await http_client.close()

                # Network errors (502/504 from request_with_retry) = RECOVERABLE
                # These are thrown ONLY for network-level issues (timeouts, connection errors)
                # NOT for HTTP-level errors (which are returned as response objects)
                if e.status_code in (502, 504):
                    # Network error → try next account
                    if account_system:
                        await account_manager.report_failure(
                            account.id, request_data.model, ErrorType.RECOVERABLE, e.status_code, None
                        )

                    last_error_message = str(e.detail)
                    last_error_status = e.status_code

                    # Single account - no point in failover, break immediately
                    if single_attempt:
                        if not account_system:
                            logger.warning("Network error (legacy mode, no failover available)")
                            if debug_logger:
                                debug_logger.flush_on_error(e.status_code, str(e.detail))
                            raise
                        break

                    logger.warning(f"Network error on account {account.id}, trying next account")
                    continue  # Try next account

                # All other HTTPException (400, 500, etc.) = application errors
                # These come from build_kiro_payload() or other places → re-raise immediately
                logger.error(f"HTTP {e.status_code} - POST /v1/chat/completions - {e.detail}")
                if debug_logger:
                    debug_logger.flush_on_error(e.status_code, str(e.detail))
                raise
            except Exception as e:
                await http_client.close()
                logger.error(f"Internal error: {e}", exc_info=True)
                logger.error(f"HTTP 500 - POST /v1/chat/completions - {str(e)[:100]}")
                if debug_logger:
                    debug_logger.flush_on_error(500, str(e))
                raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")

        # All attempts exhausted
        if single_attempt:
            # Single account - return its original error
            # last_error_status and last_error_message are guaranteed to be set
            assert last_error_status is not None
            raise HTTPException(status_code=last_error_status, detail=last_error_message)
        else:
            # Multiple accounts - every account was tried and failed
            detail = (
                f"All {len(all_accounts)} accounts failed after full circle. "
                f"Pool state: {account_manager.describe_pool_state()}."
            )
            if last_error_message:
                detail += f" Error from last account: {last_error_message}"
            raise HTTPException(status_code=503, detail=detail)
