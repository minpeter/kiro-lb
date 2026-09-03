# -*- coding: utf-8 -*-
"""
Streaming logic for converting Kiro stream to OpenAI format.

Contains generators for:
- Converting AWS SSE to OpenAI SSE
- Forming streaming chunks
- Processing tool calls in stream

Uses streaming_core.py for parsing Kiro stream into unified KiroEvent objects.
"""

import json
import re
import time
from typing import TYPE_CHECKING, Any, AsyncGenerator, Awaitable, Callable, Optional

import httpx
from fastapi import HTTPException
from loguru import logger

from kiro.config import (
    FIRST_TOKEN_MAX_RETRIES,
    FIRST_TOKEN_TIMEOUT,
)
from kiro.parsers import deduplicate_tool_calls, parse_bracket_tool_calls, tool_call_signature
from kiro.sse_validation import (
    StreamProtocolError,
    begin_openai_stream,
    end_openai_stream,
    validate_live_openai_payload,
)
from kiro.stop_reasons import is_truncated, to_openai_finish_reason

# Import from streaming_core - reuse shared parsing logic
from kiro.streaming_core import (
    FirstTokenTimeoutError,
    calculate_tokens_from_context_usage,
    parse_kiro_stream,
)
from kiro.streaming_core import (
    stream_with_first_token_retry as stream_with_first_token_retry_core,
)
from kiro.tokenizer import count_message_tokens, count_tokens, count_tools_tokens
from kiro.usage_tracking import GenerationTimer, record_token_usage, report_credits
from kiro.utils import generate_completion_id

if TYPE_CHECKING:
    from kiro.auth import KiroAuthManager
    from kiro.cache import ModelInfoCache
    from kiro.debug_logger import DebugLogger

# Import debug_logger for logging
debug_logger: Optional["DebugLogger"]
try:
    from kiro.debug_logger import debug_logger
except ImportError:
    debug_logger = None


# Re-export FirstTokenTimeoutError for backward compatibility
__all__ = [
    "FirstTokenTimeoutError",
    "stream_kiro_to_openai",
    "stream_with_first_token_retry",
    "collect_stream_response",
]


def _openai_sse(payload: dict[str, Any]) -> str:
    chunk = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    if debug_logger:
        debug_logger.log_modified_chunk(chunk.encode("utf-8"))
    try:
        validate_live_openai_payload(payload)
    except StreamProtocolError as exc:
        if debug_logger:
            debug_logger.flush_on_error(500, str(exc))
        raise
    return chunk


def _openai_done() -> str:
    chunk = "data: [DONE]\n\n"
    if debug_logger:
        debug_logger.log_modified_chunk(chunk.encode("utf-8"))
    try:
        validate_live_openai_payload(None, done=True)
    except StreamProtocolError as exc:
        if debug_logger:
            debug_logger.flush_on_error(500, str(exc))
        raise
    return chunk


async def stream_kiro_to_openai_internal(
    client: httpx.AsyncClient,
    response: httpx.Response,
    model: str,
    model_cache: "ModelInfoCache",
    auth_manager: "KiroAuthManager",
    first_token_timeout: float = FIRST_TOKEN_TIMEOUT,
    request_messages: Optional[list] = None,
    request_tools: Optional[list] = None,
    conversation_id: Optional[str] = None,
    include_reasoning: bool = True,
    parallel_tool_calls: bool = True,
) -> AsyncGenerator[str, None]:
    """
    Internal generator for converting Kiro stream to OpenAI format.

    Parses AWS SSE stream and converts events to OpenAI chat.completion.chunk.
    Supports tool calls and usage calculation.

    IMPORTANT: This function raises FirstTokenTimeoutError if first token
    is not received within first_token_timeout seconds.

    Args:
        client: HTTP client (for connection management)
        response: HTTP response with data stream
        model: Model name to include in response
        model_cache: Model cache for getting token limits
        auth_manager: Authentication manager
        first_token_timeout: First token wait timeout (seconds)
        request_messages: Original request messages (for fallback token counting)
        request_tools: Original request tools (for fallback token counting)
        conversation_id: Stable conversation ID for truncation recovery (optional)
        conversation_id: Stable conversation ID for truncation recovery (optional)

    Yields:
        Strings in SSE format: "data: {...}\\n\\n" or "data: [DONE]\\n\\n"

    Raises:
        FirstTokenTimeoutError: If first token not received within timeout

    Example:
        >>> async for chunk in stream_kiro_to_openai_internal(client, response, "claude-sonnet-4", cache, auth):
        ...     print(chunk)
        data: {"id":"chatcmpl-...","object":"chat.completion.chunk",...}

        data: [DONE]
    """
    completion_id = generate_completion_id()
    created_time = int(time.time())
    first_chunk = False
    # Timed from inside the generator: the middleware's latency stops when the
    # handler returns, which for a stream is first-byte time, so it cannot
    # measure generation.
    generation = GenerationTimer()

    metering_data = None
    context_usage_percentage = None
    full_content = ""
    full_thinking_content = ""  # Accumulated thinking content for non-streaming
    upstream_stop_reason = None
    streaming_error_occurred = False
    tool_calls_from_stream = []
    intercepted_signatures: set[str] = set()
    received_upstream_event = False

    begin_openai_stream()
    try:
        yield _openai_sse(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created_time,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                ],
            }
        )

        # Use streaming_core.parse_kiro_stream for unified event parsing
        # This handles AWS SSE parsing, first token timeout, and thinking parser
        async for event in parse_kiro_stream(response, first_token_timeout):
            received_upstream_event = True
            if event.type == "content" and event.content:
                # Accumulate content for bracket tool call detection
                full_content += event.content

                # Format as OpenAI chunk
                delta = {"content": event.content}
                if first_chunk:
                    delta["role"] = "assistant"
                    first_chunk = False

                openai_chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created_time,
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                }

                yield _openai_sse(openai_chunk)

            elif event.type == "thinking" and event.thinking_content:
                full_thinking_content += event.thinking_content
                if not include_reasoning:
                    continue
                delta = {"reasoning": event.thinking_content}
                if first_chunk:
                    delta["role"] = "assistant"
                    first_chunk = False
                openai_chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created_time,
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                }
                yield _openai_sse(openai_chunk)

            elif event.type == "tool_use" and event.tool_use:
                tool = event.tool_use

                # Extract tool name safely (handle None/missing fields)
                tool_name = ""
                if tool:
                    tool_name = (tool.get("function") or {}).get("name", "") or tool.get("name", "")

                # ==============================================================================
                # WebSearch Support - Path B: MCP Tool Emulation (Streaming Interception)
                # ==============================================================================

                # INTERCEPT web_search tool calls (Path B - MCP emulation)
                if tool_name == "web_search":
                    from kiro.mcp_tools import call_kiro_mcp_api, generate_search_summary

                    logger.info("Intercepted web_search tool call (Path B - MCP emulation)")

                    # Parse tool_input
                    tool_input = tool.get("function", {}).get("arguments", {}) or tool.get("input", {})
                    if isinstance(tool_input, str):
                        try:
                            tool_input = json.loads(tool_input)
                        except json.JSONDecodeError:
                            tool_input = {}

                    # Extract query
                    query = tool_input.get("query", "")
                    if not query:
                        logger.warning("web_search called without query, skipping MCP call")
                        # Continue with normal tool_use processing
                    else:
                        logger.debug(f"WebSearch query (Path B): {query}")

                        # Call MCP API
                        mcp_tool_use_id, results = await call_kiro_mcp_api(query, auth_manager)

                        if results is None:
                            logger.error("MCP API call failed for web_search")
                            # Continue with normal tool_use processing (will show error to user)
                        else:
                            # Emit summary as content chunks (OpenAI format)
                            summary = generate_search_summary(query, results)

                            # Send content chunks
                            chunk_size = 100
                            for i in range(0, len(summary), chunk_size):
                                content_chunk = summary[i : i + chunk_size]

                                delta = {"content": content_chunk}
                                if first_chunk:
                                    delta["role"] = "assistant"
                                    first_chunk = False

                                openai_chunk = {
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "created": created_time,
                                    "model": model,
                                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                                }

                                yield _openai_sse(openai_chunk)

                            # Accumulate for token counting
                            full_content += summary

                            # This call was answered here, so it never joins
                            # tool_calls_from_stream. Record its signature anyway:
                            # bracket recovery has to see it as already delivered,
                            # or an echo of it becomes a tool_calls delta the
                            # client executes itself, running the same search twice.
                            intercepted_signatures.add(
                                tool_call_signature(
                                    {"function": {"name": tool_name, "arguments": json.dumps(tool_input)}}
                                )
                            )

                            # Skip normal tool_use processing
                            continue

                # Collect tool calls from stream (normal tools, not web_search)
                tool_calls_from_stream.append(event.tool_use)

            elif event.type == "usage" and event.usage:
                # Kiro puts the credit cost of the request in this frame.
                report_credits(event.usage)
                metering_data = event.usage

            elif event.type == "context_usage" and event.context_usage_percentage is not None:
                context_usage_percentage = event.context_usage_percentage

            elif event.type == "stop_reason" and event.stop_reason:
                upstream_stop_reason = event.stop_reason

        if not received_upstream_event:
            raise StreamProtocolError("Upstream stream ended before any events were received")

        # Track completion signals for truncation detection
        received_usage = metering_data is not None
        received_context_usage = context_usage_percentage is not None
        stream_completed_normally = received_usage or received_context_usage

        # Check bracket-style tool calls in full content
        bracket_tool_calls = parse_bracket_tool_calls(full_content)
        all_tool_calls = tool_calls_from_stream + bracket_tool_calls
        all_tool_calls = deduplicate_tool_calls(all_tool_calls)
        # An intercepted web_search was answered above and never joined the
        # stream list, so dedup's native signatures cannot see it; drop its
        # bracket echo explicitly, mirroring the Anthropic path.
        if intercepted_signatures:
            all_tool_calls = [
                tc
                for tc in all_tool_calls
                if not (tc.get("_bracket") and tool_call_signature(tc) in intercepted_signatures)
            ]
        if not parallel_tool_calls and len(all_tool_calls) > 1:
            logger.info(f"parallel_tool_calls=false: forwarding the first of {len(all_tool_calls)} calls")
            all_tool_calls = all_tool_calls[:1]

        # Detect content truncation (missing completion signals)
        content_was_truncated = (
            not stream_completed_normally
            and len(full_content) > 0
            and not all_tool_calls  # Don't confuse with tool call truncation
        )

        if content_was_truncated:
            logger.error(
                f"Content truncated by Kiro API: stream ended without completion signals, "
                f"length={len(full_content)} chars. "
            )

        # Prefer the reason the upstream actually reported; only fall back to
        # local inference when Kiro sent no stop reason for this stream.
        upstream_finish_reason = to_openai_finish_reason(upstream_stop_reason)
        if content_was_truncated or is_truncated(upstream_stop_reason):
            finish_reason = "length"
        elif upstream_finish_reason == "tool_calls" or all_tool_calls:
            finish_reason = "tool_calls"
        elif upstream_finish_reason:
            finish_reason = upstream_finish_reason
        else:
            finish_reason = "stop"

        # Count completion_tokens (output) using tiktoken
        completion_tokens = count_tokens(full_content + full_thinking_content, model=model)

        # Calculate total_tokens based on context_usage_percentage from Kiro API
        # context_usage shows TOTAL percentage of context usage (input + output)
        prompt_tokens, total_tokens, prompt_source, total_source = calculate_tokens_from_context_usage(
            context_usage_percentage, completion_tokens, model_cache, model
        )

        # Fallback: Kiro API didn't return context_usage, use tiktoken
        # Count prompt_tokens from original messages
        # IMPORTANT: Don't apply correction coefficient for prompt_tokens,
        # as it was calibrated for completion_tokens
        if prompt_source == "unknown" and request_messages:
            prompt_tokens = count_message_tokens(request_messages, apply_claude_correction=False, model=model)
            if request_tools:
                prompt_tokens += count_tools_tokens(request_tools, apply_claude_correction=False, model=model)
            total_tokens = prompt_tokens + completion_tokens
            prompt_source = "tiktoken"
            total_source = "tiktoken"

        # Send tool calls if present
        if all_tool_calls:
            logger.debug(f"Processing {len(all_tool_calls)} tool calls for streaming response")

            # Add required index field to each tool_call
            # according to OpenAI API specification for streaming
            indexed_tool_calls = []
            for idx, tc in enumerate(all_tool_calls):
                # Extract function with None protection
                func = tc.get("function") or {}
                # Use "or" for protection against explicit None in values
                tool_name = func.get("name") or ""
                tool_args = func.get("arguments") or "{}"

                logger.debug(f"Tool call [{idx}] '{tool_name}': id={tc.get('id')}, args_length={len(tool_args)}")

                indexed_tc = {
                    "index": idx,
                    "id": tc.get("id"),
                    "type": tc.get("type", "function"),
                    "function": {"name": tool_name, "arguments": tool_args},
                }
                indexed_tool_calls.append(indexed_tc)

            tool_calls_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created_time,
                "model": model,
                "choices": [{"index": 0, "delta": {"tool_calls": indexed_tool_calls}, "finish_reason": None}],
            }
            yield _openai_sse(tool_calls_chunk)

        # Final chunk with usage
        final_chunk: dict[str, Any] = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created_time,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            },
        }

        if metering_data:
            final_chunk["usage"]["credits_used"] = metering_data

        # Log final token values being sent to client
        logger.debug(
            f"[Usage] {model}: "
            f"prompt_tokens={prompt_tokens} ({prompt_source}), "
            f"completion_tokens={completion_tokens} (tiktoken), "
            f"total_tokens={total_tokens} ({total_source})"
        )

        record_token_usage(model, prompt_tokens, completion_tokens, generation.elapsed)

        yield _openai_sse(final_chunk)
        yield _openai_done()

    except FirstTokenTimeoutError:
        # Propagate timeout up for retry
        raise
    except GeneratorExit:
        # Propagate disconnect so wrappers cannot mistake an incomplete stream
        # for a successful completion and report account success.
        logger.debug("Client disconnected (GeneratorExit)")
        streaming_error_occurred = True
        raise
    except Exception as e:
        streaming_error_occurred = True
        # Log exception type and message for better diagnostics
        error_type = type(e).__name__
        error_msg = str(e) if str(e) else "(empty message)"
        logger.error(f"Error during streaming: [{error_type}] {error_msg}", exc_info=True)
        # Propagate error up for proper handling in routes_openai.py
        raise
    finally:
        end_openai_stream()
        # Always close response
        try:
            await response.aclose()
        except Exception as close_error:
            logger.debug(f"Error closing response: {close_error}")

        if streaming_error_occurred:
            logger.debug("Streaming completed with error")
        else:
            logger.debug("Streaming completed successfully")


async def stream_kiro_to_openai(
    client: httpx.AsyncClient,
    response: httpx.Response,
    model: str,
    model_cache: "ModelInfoCache",
    auth_manager: "KiroAuthManager",
    request_messages: Optional[list] = None,
    request_tools: Optional[list] = None,
    include_reasoning: bool = True,
    parallel_tool_calls: bool = True,
) -> AsyncGenerator[str, None]:
    """
    Generator for converting Kiro stream to OpenAI format.

    This is a wrapper over stream_kiro_to_openai_internal that does NOT retry.
    Retry logic is implemented in stream_with_first_token_retry.

    Args:
        client: HTTP client (for connection management)
        response: HTTP response with data stream
        model: Model name to include in response
        model_cache: Model cache for getting token limits
        auth_manager: Authentication manager
        request_messages: Original request messages (for fallback token counting)
        request_tools: Original request tools (for fallback token counting)

    Yields:
        Strings in SSE format: "data: {...}\\n\\n" or "data: [DONE]\\n\\n"
    """
    async for chunk in stream_kiro_to_openai_internal(
        client,
        response,
        model,
        model_cache,
        auth_manager,
        request_messages=request_messages,
        request_tools=request_tools,
        include_reasoning=include_reasoning,
        parallel_tool_calls=parallel_tool_calls,
    ):
        yield chunk


async def stream_with_first_token_retry(
    make_request: Callable[[], Awaitable[httpx.Response]],
    client: httpx.AsyncClient,
    model: str,
    model_cache: "ModelInfoCache",
    auth_manager: "KiroAuthManager",
    initial_response: Optional[httpx.Response] = None,
    max_retries: int = FIRST_TOKEN_MAX_RETRIES,
    first_token_timeout: float = FIRST_TOKEN_TIMEOUT,
    request_messages: Optional[list] = None,
    request_tools: Optional[list] = None,
    include_reasoning: bool = True,
    parallel_tool_calls: bool = True,
) -> AsyncGenerator[str, None]:
    """
    Streaming with automatic retry on first token timeout.

    If model doesn't respond within first_token_timeout seconds,
    request is cancelled and a new one is made. Maximum max_retries attempts.

    This is seamless for user - they just see a delay,
    but eventually get a response (or error after all attempts).

    Uses generic stream_with_first_token_retry from streaming_core.py.

    Args:
        make_request: Function to create new HTTP request
        client: HTTP client
        model: Model name
        model_cache: Model cache
        auth_manager: Authentication manager
        initial_response: Optional pre-validated response to use on first attempt.
                         If provided, make_request is only called on retries.
        max_retries: Maximum number of attempts
        first_token_timeout: First token wait timeout (seconds)
        request_messages: Original request messages (for fallback token counting)
        request_tools: Original request tools (for fallback token counting)

    Yields:
        Strings in SSE format

    Raises:
        HTTPException: After exhausting all attempts

    Example:
        >>> async def make_req():
        ...     return await http_client.request_with_retry("POST", url, payload, stream=True)
        >>> response = await make_req()
        >>> async for chunk in stream_with_first_token_retry(
        ...     make_req, client, model, cache, auth, initial_response=response
        ... ):
        ...     print(chunk)
    """

    def create_http_error(status_code: int, error_text: str) -> HTTPException:
        """Create HTTPException for HTTP errors."""
        return HTTPException(status_code=status_code, detail=f"Upstream API error: {error_text}")

    def create_timeout_error(retries: int, timeout: float) -> HTTPException:
        """Create HTTPException for timeout errors."""
        return HTTPException(
            status_code=504,
            detail=f"Model did not respond within {timeout}s after {retries} attempts. Please try again.",
        )

    async def stream_processor(response: httpx.Response) -> AsyncGenerator[str, None]:
        """Process response and yield OpenAI SSE chunks."""
        async for chunk in stream_kiro_to_openai_internal(
            client,
            response,
            model,
            model_cache,
            auth_manager,
            first_token_timeout=first_token_timeout,
            request_messages=request_messages,
            request_tools=request_tools,
            include_reasoning=include_reasoning,
            parallel_tool_calls=parallel_tool_calls,
        ):
            yield chunk

    async for chunk in stream_with_first_token_retry_core(
        make_request=make_request,
        stream_processor=stream_processor,
        initial_response=initial_response,
        max_retries=max_retries,
        first_token_timeout=first_token_timeout,
        on_http_error=create_http_error,
        on_all_retries_failed=create_timeout_error,
    ):
        yield chunk


# (?!\w) after the optional language tag rejects ```python / ```jsonx: only a
# bare fence or an explicit json tag counts as a JSON wrapper.
_JSON_FENCE_RE = re.compile(r"\A```(?:json)?(?!\w)\s*(.*?)\s*```\Z", re.IGNORECASE | re.DOTALL)


def strip_json_code_fence(text: str) -> str:
    """
    Removes one markdown code fence wrapping the entire content.

    Only fires when the trimmed text is exactly one ```json ... ``` (or bare
    ``` ... ```) block, which is what models emit when the response_format
    prompt directive is only partially obeyed. Leading/trailing prose or a
    mid-text fence leaves the content untouched: rewriting those would corrupt
    legitimate markdown the caller asked for.

    Args:
        text: Full assistant content

    Returns:
        The unwrapped JSON, or the original text when no full-wrap fence exists
    """
    match = _JSON_FENCE_RE.match(text.strip())
    if not match:
        return text
    inner = match.group(1).strip()
    return inner or text


async def collect_stream_response(
    client: httpx.AsyncClient,
    response: httpx.Response,
    model: str,
    model_cache: "ModelInfoCache",
    auth_manager: "KiroAuthManager",
    request_messages: Optional[list] = None,
    request_tools: Optional[list] = None,
    include_reasoning: bool = True,
    parallel_tool_calls: bool = True,
    strip_json_fence: bool = False,
) -> dict:
    """
    Collect full response from streaming stream.

    Used for non-streaming mode - collects all chunks
    and forms a single response.

    Args:
        client: HTTP client
        response: HTTP response with stream
        model: Model name
        model_cache: Model cache
        auth_manager: Authentication manager
        request_messages: Original request messages (for fallback token counting)
        request_tools: Original request tools (for fallback token counting)

    Returns:
        Dictionary with full response in OpenAI chat.completion format
    """
    full_content = ""
    full_reasoning_content = ""
    final_usage = None
    tool_calls = []
    finish_reason = "stop"  # Default fallback
    completion_id = generate_completion_id()

    async for chunk_str in stream_kiro_to_openai(
        client,
        response,
        model,
        model_cache,
        auth_manager,
        request_messages=request_messages,
        request_tools=request_tools,
        include_reasoning=include_reasoning,
        parallel_tool_calls=parallel_tool_calls,
    ):
        if not chunk_str.startswith("data:"):
            continue

        data_str = chunk_str[len("data:") :].strip()
        if not data_str or data_str == "[DONE]":
            continue

        try:
            chunk_data = json.loads(data_str)

            # Extract data from chunk
            delta = chunk_data.get("choices", [{}])[0].get("delta", {})
            if "content" in delta:
                full_content += delta["content"]
            if "reasoning" in delta:
                full_reasoning_content += delta["reasoning"]
            if "tool_calls" in delta:
                tool_calls.extend(delta["tool_calls"])

            # Extract finish_reason from chunk (streaming already calculated it correctly)
            finish_reason_from_chunk = chunk_data.get("choices", [{}])[0].get("finish_reason")
            if finish_reason_from_chunk:
                finish_reason = finish_reason_from_chunk

            # Save usage from last chunk
            if "usage" in chunk_data:
                final_usage = chunk_data["usage"]

        except (json.JSONDecodeError, IndexError):
            continue

    # Form final response
    # Only when the caller asked for JSON output: a model that ignored the
    # directive and fenced its JSON would otherwise fail the client's parse.
    if strip_json_fence and full_content:
        full_content = strip_json_code_fence(full_content)

    message: dict[str, Any] = {"role": "assistant", "content": full_content}
    if full_reasoning_content:
        message["reasoning"] = full_reasoning_content
    if tool_calls:
        # For non-streaming response remove index field from tool_calls,
        # as it's only required for streaming chunks
        cleaned_tool_calls = []
        for tc in tool_calls:
            # Extract function with None protection
            func = tc.get("function") or {}
            cleaned_tc = {
                "id": tc.get("id"),
                "type": tc.get("type", "function"),
                "function": {"name": func.get("name", ""), "arguments": func.get("arguments", "{}")},
            }
            cleaned_tool_calls.append(cleaned_tc)
        message["tool_calls"] = cleaned_tool_calls

    # Form usage for response
    usage = final_usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    # Log token info for debugging (non-streaming uses same logs from streaming)

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage,
    }
