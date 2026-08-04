# -*- coding: utf-8 -*-
"""
FastAPI routes for Anthropic Messages API.

Contains the /v1/messages endpoint compatible with Anthropic's Messages API.

Reference: https://docs.anthropic.com/en/api/messages
"""

import json
from typing import TYPE_CHECKING, Any, Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Security
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from loguru import logger

from kiro.auth import AuthType
from kiro.config import PROFILE_ARN, WEB_SEARCH_ENABLED
from kiro.converters_anthropic import anthropic_to_kiro
from kiro.dashboard import identify_data_api_key
from kiro.http_client import KiroHttpClient
from kiro.models_anthropic import (
    AnthropicCountTokensRequest,
    AnthropicMessage,
    AnthropicMessagesRequest,
    ToolResultContentBlock,
    ToolUseContentBlock,
)
from kiro.payload_guards import PayloadTooLargeError
from kiro.streaming_anthropic import (
    collect_anthropic_response,
    stream_with_first_token_retry_anthropic,
)
from kiro.tokenizer import estimate_request_tokens
from kiro.usage_tracking import current_api_key_id
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
# Anthropic uses x-api-key header instead of Authorization: Bearer
anthropic_api_key_header = APIKeyHeader(name="x-api-key", auto_error=False)
# Also support Authorization: Bearer for compatibility
auth_header = APIKeyHeader(name="Authorization", auto_error=False)


WEB_SEARCH_DESCRIPTION = "Search the web for current information. Use when you need up-to-date data from the internet."
WEB_SEARCH_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string", "description": "Search query"}},
    "required": ["query"],
}


def normalize_native_web_search_tools(tools: Optional[list]) -> None:
    """Give native server-side ``web_search`` tools a schema Kiro can act on.

    An Anthropic native tool (``type="web_search_20250305"``) carries no
    ``input_schema`` because Anthropic supplies it server-side. This gateway instead
    advertises the tool to Kiro and intercepts the call mid-stream, so without a schema
    the model is handed a tool with no ``query`` parameter to populate.
    """
    for tool in tools or []:
        tool_type = getattr(tool, "type", None)
        if not (tool_type and tool_type.startswith("web_search")):
            continue
        if not getattr(tool, "input_schema", None):
            tool.input_schema = dict(WEB_SEARCH_INPUT_SCHEMA)
        if not (getattr(tool, "description", None) or "").strip():
            tool.description = WEB_SEARCH_DESCRIPTION
        logger.debug("Normalized native web_search tool for mid-stream interception (Path A)")


async def verify_anthropic_api_key(
    x_api_key: Optional[str] = Security(anthropic_api_key_header), authorization: Optional[str] = Security(auth_header)
) -> bool:
    """
    Verify API key for Anthropic API.

    Supports two authentication methods:
    1. x-api-key header (Anthropic native)
    2. Authorization: Bearer header (for compatibility)

    Args:
        x_api_key: Value from x-api-key header
        authorization: Value from Authorization header

    Returns:
        True if key is valid

    Raises:
        HTTPException: 401 if key is invalid or missing
    """
    # Check x-api-key first (Anthropic native), then Bearer compatibility.
    candidate = x_api_key or (
        authorization.removeprefix("Bearer ") if authorization and authorization.startswith("Bearer ") else ""
    )
    if candidate:
        key_id = identify_data_api_key(candidate)
        if key_id is not None:
            current_api_key_id.set(key_id)
            return True

    logger.warning("Access attempt with invalid API key (Anthropic endpoint)")
    raise HTTPException(
        status_code=401,
        detail={
            "type": "error",
            "error": {
                "type": "authentication_error",
                "message": "Invalid or missing API key. Use x-api-key header or Authorization: Bearer.",
            },
        },
    )


# --- Router ---
router = APIRouter(tags=["Anthropic API"])


@router.post("/v1/messages", dependencies=[Depends(verify_anthropic_api_key)])
async def messages(
    request: Request,
    request_data: AnthropicMessagesRequest,
    anthropic_version: Optional[str] = Header(None, alias="anthropic-version"),
):
    """
    Anthropic Messages API endpoint.

    Compatible with Anthropic's /v1/messages endpoint.
    Accepts requests in Anthropic format and translates them to Kiro API.

    Required headers:
    - x-api-key: Your API key (or Authorization: Bearer)
    - anthropic-version: API version (optional, for compatibility)
    - Content-Type: application/json

    Args:
        request: FastAPI Request for accessing app.state
        request_data: Request in Anthropic MessagesRequest format
        anthropic_version: Anthropic API version header (optional)

    Returns:
        StreamingResponse for streaming mode (SSE)
        JSONResponse for non-streaming mode

    Raises:
        HTTPException: On validation or API errors
    """
    logger.info(f"Request to /v1/messages (model={request_data.model}, stream={request_data.stream})")

    if anthropic_version:
        logger.debug(f"Anthropic-Version header: {anthropic_version}")

    # Note: prepare_new_request() and log_request_body() are now called by DebugLoggerMiddleware
    # This ensures debug logging works even for requests that fail Pydantic validation (422 errors)

    # ==============================================================================
    # WebSearch Support - Path B: Auto-Injection (MCP Tool Emulation)
    # ==============================================================================

    # Auto-inject web_search tool if enabled (Path B - MCP emulation)
    if WEB_SEARCH_ENABLED:
        if request_data.tools is None:
            request_data.tools = []

        # Check if web_search already exists (by name)
        has_ws = any(getattr(tool, "name", "") == "web_search" for tool in request_data.tools)

        if not has_ws:
            from kiro.models_anthropic import AnthropicTool

            web_search_tool = AnthropicTool(
                name="web_search",
                description=WEB_SEARCH_DESCRIPTION,
                input_schema=dict(WEB_SEARCH_INPUT_SCHEMA),
            )
            request_data.tools.append(web_search_tool)
            logger.debug("Auto-injected web_search tool for MCP emulation (Path B)")

    normalize_native_web_search_tools(request_data.tools)

    # ==============================================================================
    # Account System: Account System Failover or Legacy Mode
    # ==============================================================================

    if request.app.state.account_system:
        # ==============================================================================
        # ACCOUNT SYSTEM ENABLED: Failover Loop
        # ==============================================================================
        from kiro.account_errors import ErrorType, classify_error

        account_manager = request.app.state.account_manager
        all_accounts = list(account_manager._accounts.keys())
        MAX_ATTEMPTS = len(all_accounts) * 2  # Full circle with margin

        last_error_message = None
        last_error_status = None
        tried_accounts: set[str] = set()  # Track tried accounts in current failover loop

        for attempt in range(MAX_ATTEMPTS):
            # Get next available account (excluding already tried)
            account = await account_manager.get_next_account(request_data.model, exclude_accounts=tried_accounts)

            if account is None:
                # All accounts unavailable
                if len(all_accounts) == 1:
                    # Single account - return original error with original status code
                    return JSONResponse(
                        status_code=last_error_status or 503,
                        content={
                            "type": "error",
                            "error": {"type": "api_error", "message": last_error_message or "Account unavailable"},
                        },
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
                    return JSONResponse(
                        status_code=503, content={"type": "error", "error": {"type": "api_error", "message": detail}}
                    )

            # Mark account as tried in current failover loop
            tried_accounts.add(account.id)

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
                kiro_payload = anthropic_to_kiro(request_data, conversation_id, profile_arn_for_payload)
            except PayloadTooLargeError as e:
                logger.error(f"Payload too large: {e}")
                return JSONResponse(
                    status_code=400,
                    content={"type": "error", "error": {"type": "invalid_request_error", "message": str(e)}},
                )
            except ValueError as e:
                logger.error(f"Conversion error: {e}")
                return JSONResponse(
                    status_code=400,
                    content={"type": "error", "error": {"type": "invalid_request_error", "message": str(e)}},
                )

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

            # Prepare data for token counting
            messages_for_tokenizer = [msg.model_dump() for msg in request_data.messages]
            tools_for_tokenizer = [tool.model_dump() for tool in request_data.tools] if request_data.tools else None
            system_for_tokenizer: str | list[dict[str, Any]] | None
            if isinstance(request_data.system, list):
                system_for_tokenizer = [b.model_dump() if hasattr(b, "model_dump") else b for b in request_data.system]
            else:
                system_for_tokenizer = request_data.system

            try:
                # Make request to Kiro API
                response = await http_client.request_with_retry(
                    "POST", url, kiro_payload, stream=True, retry_rate_limits=False
                )

                if response.status_code == 200:
                    # SUCCESS - report and return
                    await account_manager.report_success(account.id, request_data.model)

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

                                async for chunk in stream_with_first_token_retry_anthropic(
                                    make_request=make_retry_request,
                                    model=request_data.model,
                                    model_cache=model_cache,
                                    auth_manager=auth_manager,
                                    initial_response=response,
                                    request_messages=messages_for_tokenizer,
                                    request_tools=tools_for_tokenizer,
                                    request_system=system_for_tokenizer,
                                ):
                                    yield chunk
                            except GeneratorExit:
                                client_disconnected = True
                                logger.debug("Client disconnected during streaming (GeneratorExit in routes)")
                            except Exception as e:
                                streaming_error = e
                                try:
                                    error_event = f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': str(e)}})}\n\n"
                                    yield error_event
                                except Exception:
                                    pass
                            finally:
                                await http_client.close()
                                if streaming_error:
                                    error_type = type(streaming_error).__name__
                                    error_msg = str(streaming_error) if str(streaming_error) else "(empty message)"
                                    logger.error(
                                        f"HTTP 500 - POST /v1/messages (streaming) - [{error_type}] {error_msg[:100]}"
                                    )
                                elif client_disconnected:
                                    logger.info("HTTP 200 - POST /v1/messages (streaming) - client disconnected")
                                else:
                                    logger.info("HTTP 200 - POST /v1/messages (streaming) - completed")

                                if debug_logger:
                                    if streaming_error:
                                        debug_logger.flush_on_error(500, str(streaming_error))
                                    else:
                                        debug_logger.discard_buffers()

                        return StreamingResponse(
                            stream_wrapper(),
                            media_type="text/event-stream",
                            headers={
                                "Cache-Control": "no-cache",
                                "Connection": "keep-alive",
                            },
                        )

                    else:
                        # Non-streaming mode
                        anthropic_response = await collect_anthropic_response(
                            response,
                            request_data.model,
                            model_cache,
                            auth_manager,
                            request_messages=messages_for_tokenizer,
                            request_tools=tools_for_tokenizer,
                            request_system=system_for_tokenizer,
                        )

                        await http_client.close()
                        logger.info("HTTP 200 - POST /v1/messages (non-streaming) - completed")

                        if debug_logger:
                            debug_logger.discard_buffers()

                        return JSONResponse(content=anthropic_response)

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
                        await account_manager.report_failure(
                            account.id,
                            request_data.model,
                            error_type,
                            response.status_code,
                            error_reason,
                            upstream_message,
                        )

                        logger.warning(f"HTTP {response.status_code} - POST /v1/messages - {last_error_message[:100]}")

                        if debug_logger:
                            debug_logger.flush_on_error(response.status_code, last_error_message)

                        return JSONResponse(
                            status_code=response.status_code,
                            content={"type": "error", "error": {"type": "api_error", "message": last_error_message}},
                        )

                    else:  # ErrorType.RECOVERABLE
                        # RECOVERABLE - try next account
                        await account_manager.report_failure(
                            account.id,
                            request_data.model,
                            error_type,
                            response.status_code,
                            error_reason,
                            upstream_message,
                        )

                        # Single account - no point in failover, break immediately
                        if len(all_accounts) == 1:
                            break

                        continue  # Next iteration

            except HTTPException as e:
                await http_client.close()

                # Network errors (502/504 from request_with_retry) = RECOVERABLE
                # These are thrown ONLY for network-level issues (timeouts, connection errors)
                # NOT for HTTP-level errors (which are returned as response objects)
                if e.status_code in (502, 504):
                    # Network error → try next account
                    await account_manager.report_failure(
                        account.id, request_data.model, ErrorType.RECOVERABLE, e.status_code, None
                    )

                    last_error_message = str(e.detail)
                    last_error_status = e.status_code

                    # Single account - no point in failover, break immediately
                    if len(all_accounts) == 1:
                        break

                    logger.warning(f"Network error on account {account.id}, trying next account")
                    continue  # Try next account

                # All other HTTPException (400, 500, etc.) = application errors
                # These come from build_kiro_payload() or other places → re-raise immediately
                logger.error(f"HTTP {e.status_code} - POST /v1/messages - {e.detail}")
                if debug_logger:
                    debug_logger.flush_on_error(e.status_code, str(e.detail))
                raise
            except Exception as e:
                await http_client.close()
                logger.error(f"Internal error: {e}", exc_info=True)
                logger.error(f"HTTP 500 - POST /v1/messages - {str(e)[:100]}")
                if debug_logger:
                    debug_logger.flush_on_error(500, str(e))

                return JSONResponse(
                    status_code=500,
                    content={
                        "type": "error",
                        "error": {"type": "api_error", "message": f"Internal Server Error: {str(e)}"},
                    },
                )

        # All attempts exhausted
        if len(all_accounts) == 1:
            # Single account - return its original error
            # last_error_status and last_error_message are guaranteed to be set
            assert last_error_status is not None
            return JSONResponse(
                status_code=last_error_status,
                content={"type": "error", "error": {"type": "api_error", "message": last_error_message}},
            )
        else:
            # Multiple accounts - every account was tried and failed
            detail = (
                f"All {len(all_accounts)} accounts failed after full circle. "
                f"Pool state: {account_manager.describe_pool_state()}."
            )
            if last_error_message:
                detail += f" Error from last account: {last_error_message}"
            return JSONResponse(
                status_code=503, content={"type": "error", "error": {"type": "api_error", "message": detail}}
            )

    else:
        # ==============================================================================
        # LEGACY MODE: Single Account (no failover)
        # ==============================================================================
        account = request.app.state.account_manager.get_first_account()
        if not account.auth_manager:
            logger.error("No initialized accounts available (legacy mode)")
            return JSONResponse(
                status_code=503,
                content={
                    "type": "error",
                    "error": {"type": "api_error", "message": "No initialized accounts available"},
                },
            )
        auth_manager = account.auth_manager
        model_cache = account.model_cache

    # ==============================================================================
    # Normal Flow (Path B will be intercepted in streaming, or no web_search)
    # ==============================================================================

    # Generate conversation ID for Kiro API (random UUID, not used for tracking)
    conversation_id = generate_conversation_id()

    # Build payload for Kiro
    # A Builder ID account has no profile and must not be given one: the global
    # fallback would send a foreign ARN and fail the request.
    profile_arn_for_payload = auth_manager.profile_arn or (
        "" if auth_manager.auth_type == AuthType.AWS_SSO_OIDC else PROFILE_ARN or ""
    )

    try:
        kiro_payload = anthropic_to_kiro(request_data, conversation_id, profile_arn_for_payload)
    except PayloadTooLargeError as e:
        logger.error(f"Payload too large: {e}")
        return JSONResponse(
            status_code=400, content={"type": "error", "error": {"type": "invalid_request_error", "message": str(e)}}
        )
    except ValueError as e:
        logger.error(f"Conversion error: {e}")
        return JSONResponse(
            status_code=400, content={"type": "error", "error": {"type": "invalid_request_error", "message": str(e)}}
        )

    # Log Kiro payload
    try:
        kiro_request_body = json.dumps(kiro_payload, ensure_ascii=False, indent=2).encode("utf-8")
        if debug_logger:
            debug_logger.log_kiro_request_body(kiro_request_body)
    except Exception as e:
        logger.warning(f"Failed to log Kiro request: {e}")

    # Create HTTP client with retry logic
    # For streaming: use per-request client to avoid CLOSE_WAIT leak on VPN disconnect (issue #54)
    # For non-streaming: use shared client for connection pooling
    url = f"{auth_manager.api_host}/generateAssistantResponse"
    logger.debug(f"Kiro API URL: {url}")

    if request_data.stream:
        # Streaming mode: per-request client prevents orphaned connections
        # when network interface changes (VPN disconnect/reconnect)
        http_client = KiroHttpClient(auth_manager, shared_client=None)
    else:
        # Non-streaming mode: shared client for efficient connection reuse
        shared_client = request.app.state.http_client
        http_client = KiroHttpClient(auth_manager, shared_client=shared_client)

    # Prepare data for token counting
    # Convert Pydantic models to dicts for tokenizer
    messages_for_tokenizer = [msg.model_dump() for msg in request_data.messages]
    tools_for_tokenizer = [tool.model_dump() for tool in request_data.tools] if request_data.tools else None
    # Serialize system prompt (may be a list of Pydantic objects)
    if isinstance(request_data.system, list):
        system_for_tokenizer = [b.model_dump() if hasattr(b, "model_dump") else b for b in request_data.system]
    else:
        system_for_tokenizer = request_data.system

    async def make_search_request(tool_use_id: str, query: str, result_content: str) -> httpx.Response:
        followup_request = request_data.model_copy(deep=True)
        followup_request.messages.extend(
            [
                AnthropicMessage(
                    role="assistant",
                    content=[ToolUseContentBlock(id=tool_use_id, name="web_search", input={"query": query})],
                ),
                AnthropicMessage(
                    role="user",
                    content=[ToolResultContentBlock(tool_use_id=tool_use_id, content=result_content)],
                ),
            ]
        )
        followup_payload = anthropic_to_kiro(followup_request, conversation_id, profile_arn_for_payload)
        return await http_client.request_with_retry("POST", url, followup_payload, stream=True, retry_rate_limits=False)

    try:
        # Make request to Kiro API (for both streaming and non-streaming modes)
        # Important: we wait for Kiro response BEFORE returning StreamingResponse,
        # so that we can return proper HTTP error codes if Kiro fails
        response = await http_client.request_with_retry("POST", url, kiro_payload, stream=True, retry_rate_limits=False)

        if response.status_code != 200:
            try:
                error_content = await response.aread()
            except Exception:
                error_content = b"Unknown error"

            await http_client.close()
            error_text = error_content.decode("utf-8", errors="replace")

            # Try to parse JSON response from Kiro to extract error message
            error_message = error_text
            try:
                error_json = json.loads(error_text)
                # Enhance Kiro API errors with user-friendly messages
                from kiro.kiro_errors import enhance_kiro_error

                error_info = enhance_kiro_error(error_json)
                error_message = error_info.user_message
                # Log original error for debugging
                logger.debug(f"Original Kiro error: {error_info.original_message} (reason: {error_info.reason})")
            except (json.JSONDecodeError, KeyError):
                pass

            # Log access log for error (before flush, so it gets into app_logs)
            logger.warning(f"HTTP {response.status_code} - POST /v1/messages - {error_message[:100]}")

            # Flush debug logs on error
            if debug_logger:
                debug_logger.flush_on_error(response.status_code, error_message)

            # Return error in Anthropic format
            return JSONResponse(
                status_code=response.status_code,
                content={"type": "error", "error": {"type": "api_error", "message": error_message}},
            )

        if request_data.stream:
            # Streaming mode with first token retry
            async def stream_wrapper():
                streaming_error = None
                client_disconnected = False
                try:
                    # Create retry request function for retries
                    async def make_retry_request():
                        return await http_client.request_with_retry(
                            "POST", url, kiro_payload, stream=True, retry_rate_limits=False
                        )

                    # Use retry wrapper with initial response
                    async for chunk in stream_with_first_token_retry_anthropic(
                        make_request=make_retry_request,
                        model=request_data.model,
                        model_cache=model_cache,
                        auth_manager=auth_manager,
                        initial_response=response,
                        request_messages=messages_for_tokenizer,
                        request_tools=tools_for_tokenizer,
                        request_system=system_for_tokenizer,
                        make_search_request=make_search_request,
                    ):
                        yield chunk
                except GeneratorExit:
                    client_disconnected = True
                    logger.debug("Client disconnected during streaming (GeneratorExit in routes)")
                except Exception as e:
                    streaming_error = e
                    # Send error event to client, then gracefully end the stream
                    try:
                        error_event = f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': str(e)}})}\n\n"
                        yield error_event
                    except Exception:
                        pass
                finally:
                    await http_client.close()
                    if streaming_error:
                        error_type = type(streaming_error).__name__
                        error_msg = str(streaming_error) if str(streaming_error) else "(empty message)"
                        logger.error(f"HTTP 500 - POST /v1/messages (streaming) - [{error_type}] {error_msg[:100]}")
                    elif client_disconnected:
                        logger.info("HTTP 200 - POST /v1/messages (streaming) - client disconnected")
                    else:
                        logger.info("HTTP 200 - POST /v1/messages (streaming) - completed")

                    if debug_logger:
                        if streaming_error:
                            debug_logger.flush_on_error(500, str(streaming_error))
                        else:
                            debug_logger.discard_buffers()

            return StreamingResponse(
                stream_wrapper(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )

        else:
            # Non-streaming mode - collect entire response
            anthropic_response = await collect_anthropic_response(
                response,
                request_data.model,
                model_cache,
                auth_manager,
                request_messages=messages_for_tokenizer,
                request_tools=tools_for_tokenizer,
                request_system=system_for_tokenizer,
                make_search_request=make_search_request,
            )

            await http_client.close()

            logger.info("HTTP 200 - POST /v1/messages (non-streaming) - completed")

            if debug_logger:
                debug_logger.discard_buffers()

            return JSONResponse(content=anthropic_response)

    except HTTPException as e:
        await http_client.close()

        # Network errors (502/504 from request_with_retry) = RECOVERABLE
        # In legacy mode, we still log them but re-raise (no failover available)
        if e.status_code in (502, 504):
            logger.warning("Network error (legacy mode, no failover available)")

        logger.error(f"HTTP {e.status_code} - POST /v1/messages - {e.detail}")
        if debug_logger:
            debug_logger.flush_on_error(e.status_code, str(e.detail))
        raise
    except Exception as e:
        await http_client.close()
        logger.error(f"Internal error: {e}", exc_info=True)
        logger.error(f"HTTP 500 - POST /v1/messages - {str(e)[:100]}")
        if debug_logger:
            debug_logger.flush_on_error(500, str(e))

        return JSONResponse(
            status_code=500,
            content={"type": "error", "error": {"type": "api_error", "message": f"Internal Server Error: {str(e)}"}},
        )


@router.post("/v1/messages/count_tokens", dependencies=[Depends(verify_anthropic_api_key)])
async def count_tokens_endpoint(
    request: Request,
    request_data: AnthropicCountTokensRequest,
):
    """
    Anthropic Count Tokens API endpoint.

    Returns estimated token count for the given request payload.
    Used by Claude Code to decide when to trigger conversation compaction.

    Uses the same fallback estimation as Anthropic streaming (message_start event),
    since Kiro API only provides accurate token counts after request completion.
    This endpoint is called BEFORE the actual request, so we cannot use Kiro's
    contextUsagePercentage (which is only available after generation completes).

    Args:
        request: FastAPI Request for accessing app.state
        request_data: Request in Anthropic MessagesRequest format

    Returns:
        JSONResponse with {"input_tokens": int}

    Raises:
        HTTPException: 401 if authentication fails (handled by dependency)
    """
    logger.info(
        f"Request to /v1/messages/count_tokens (model={request_data.model}, messages={len(request_data.messages)})"
    )

    # Prepare data for tokenizer (same format as streaming message_start)
    messages_for_tokenizer = [msg.model_dump() for msg in request_data.messages]
    tools_for_tokenizer = [tool.model_dump() for tool in request_data.tools] if request_data.tools else None

    # Handle system prompt (can be string or list of content blocks)
    system_for_tokenizer: str | list[dict[str, Any]] | None
    if isinstance(request_data.system, list):
        system_for_tokenizer = [b.model_dump() if hasattr(b, "model_dump") else b for b in request_data.system]
    else:
        system_for_tokenizer = request_data.system

    # Use the SAME estimation logic as Anthropic streaming message_start
    request_token_stats = estimate_request_tokens(
        messages=messages_for_tokenizer,
        tools=tools_for_tokenizer,
        system_prompt=system_for_tokenizer,
        apply_claude_correction=True,  # CRITICAL: Enable correction for Claude models
        model=request_data.model,
    )

    input_tokens = request_token_stats["total_tokens"]

    logger.info(f"Token count estimate: {input_tokens} tokens")

    return JSONResponse(content={"input_tokens": input_tokens})
