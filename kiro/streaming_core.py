# -*- coding: utf-8 -*-
"""
Core streaming logic for parsing Kiro API responses.

This module contains shared logic used by both OpenAI and Anthropic streaming:
- KiroEvent dataclass for unified events
- Kiro SSE stream parsing
- Full response collection
- First token timeout handling

The core layer provides a unified interface that API-specific formatters use
to convert Kiro events to their respective SSE formats.
"""

import asyncio
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, AsyncGenerator, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx
from loguru import logger

from kiro.config import (
    FIRST_TOKEN_MAX_RETRIES,
    FIRST_TOKEN_TIMEOUT,
)
from kiro.parsers import (
    AwsEventStreamParser,
    MalformedToolInputError,
    deduplicate_tool_calls,
    parse_bracket_tool_calls,
    split_bracket_call_text,
)
from kiro.sse_validation import StreamProtocolError

if TYPE_CHECKING:
    from kiro.cache import ModelInfoCache
    from kiro.debug_logger import DebugLogger

# Import debug_logger for logging
debug_logger: Optional["DebugLogger"]
try:
    from kiro.debug_logger import debug_logger
except ImportError:
    debug_logger = None


# ==================================================================================================
# Data Classes
# ==================================================================================================


@dataclass
class KiroEvent:
    """
    Unified event from Kiro API stream.

    This format is API-agnostic and can be converted to both OpenAI and Anthropic formats.

    Attributes:
        type: Event type (content, thinking, tool_use, usage, context_usage, error)
        content: Text content (for content events)
        thinking_content: Thinking/reasoning content (for thinking events)
        thinking_signature: Opaque upstream signature for a thinking block
        tool_use: Tool use data (for tool_use events)
        tool_use_id: Upstream id of the tool call being streamed
        tool_use_name: Name of the tool call being streamed
        tool_input_delta: One raw fragment of the tool arguments, as sent upstream
        usage: Usage/metering data (for usage events)
        context_usage_percentage: Context usage percentage (for context_usage events)
        is_first_thinking_chunk: Whether this is the first thinking chunk
        is_last_thinking_chunk: Whether this is the last thinking chunk
        stop_reason: Upstream stop reason (for stop_reason events)
    """

    type: str
    content: Optional[str] = None
    thinking_content: Optional[str] = None
    thinking_signature: Optional[str] = None
    tool_use: Optional[Dict[str, Any]] = None
    tool_use_id: Optional[str] = None
    tool_use_name: Optional[str] = None
    tool_input_delta: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    context_usage_percentage: Optional[float] = None
    is_first_thinking_chunk: bool = False
    is_last_thinking_chunk: bool = False
    stop_reason: Optional[str] = None


@dataclass
class StreamResult:
    """
    Result of collecting a complete stream response.

    Attributes:
        content: Full text content
        thinking_content: Full thinking/reasoning content
        thinking_signature: Opaque upstream signature for the thinking block
        content_blocks: Ordered native thinking, text, and tool blocks
        tool_calls: List of tool calls
        usage: Usage information
        context_usage_percentage: Context usage percentage from Kiro API
        stop_reason: Upstream stop reason, when the stream reported one
    """

    content: str = ""
    thinking_content: str = ""
    thinking_signature: str = ""
    content_blocks: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    usage: Optional[Dict[str, Any]] = None
    context_usage_percentage: Optional[float] = None
    stop_reason: Optional[str] = None


class FirstTokenTimeoutError(Exception):
    """Exception raised when first token timeout occurs."""

    pass


# ==================================================================================================
# Kiro Stream Parsing
# ==================================================================================================


async def parse_kiro_stream(
    response: httpx.Response,
    first_token_timeout: float = FIRST_TOKEN_TIMEOUT,
) -> AsyncGenerator[KiroEvent, None]:
    """Yield only structured events actually received from the Kiro upstream.

    kiro-lb deliberately does not manufacture thinking/reasoning blocks from
    prompt tags or response text.  Content is passed through verbatim.
    """
    parser = AwsEventStreamParser()
    received_event = False
    try:
        byte_iterator = response.aiter_bytes()
        try:
            first_chunk = await asyncio.wait_for(byte_iterator.__anext__(), timeout=first_token_timeout)
        except asyncio.TimeoutError:
            raise FirstTokenTimeoutError(f"No response within {first_token_timeout} seconds")
        except StopAsyncIteration:
            raise StreamProtocolError("Upstream stream ended before any events were received")

        if debug_logger:
            debug_logger.log_raw_chunk(first_chunk)
        async for event in _process_chunk(parser, first_chunk):
            received_event = True
            if debug_logger:
                debug_logger.log_parsed_event(asdict(event))
            yield event
        if debug_logger:
            for frame in parser.drain_observed_frames():
                debug_logger.log_parsed_event(
                    {
                        "type": "raw_upstream_frame",
                        "frame": frame,
                    }
                )
        async for chunk in byte_iterator:
            if debug_logger:
                debug_logger.log_raw_chunk(chunk)
            async for event in _process_chunk(parser, chunk):
                received_event = True
                if debug_logger:
                    debug_logger.log_parsed_event(asdict(event))
                yield event
            if debug_logger:
                for frame in parser.drain_observed_frames():
                    debug_logger.log_parsed_event(
                        {
                            "type": "raw_upstream_frame",
                            "frame": frame,
                        }
                    )
        for tool_call in parser.get_unemitted_tool_calls():
            if tool_call.get("_parse_error"):
                raise MalformedToolInputError("Malformed upstream tool input")
            event = KiroEvent(type="tool_use", tool_use=tool_call)
            received_event = True
            if debug_logger:
                debug_logger.log_parsed_event(asdict(event))
            yield event
        if not received_event:
            raise StreamProtocolError("Upstream stream ended before any events were received")
    except FirstTokenTimeoutError:
        raise
    except GeneratorExit:
        raise
    except Exception as exc:
        logger.error("Error during stream parsing: {}", exc, exc_info=True)
        raise


async def _process_chunk(parser: AwsEventStreamParser, chunk: bytes) -> AsyncGenerator[KiroEvent, None]:
    """Translate Kiro upstream stream frames without text-level interpretation."""
    for event in parser.feed(chunk):
        if event["type"] == "content":
            yield KiroEvent(type="content", content=event["data"])
        elif event["type"] == "usage":
            yield KiroEvent(type="usage", usage=event["data"])
        elif event["type"] == "context_usage":
            yield KiroEvent(type="context_usage", context_usage_percentage=event["data"])
        elif event["type"] == "stop_reason":
            yield KiroEvent(type="stop_reason", stop_reason=event["data"])
        elif event["type"] == "native_thinking":
            if event["data"]:
                yield KiroEvent(
                    type="thinking",
                    thinking_content=event["data"],
                    is_first_thinking_chunk=event.get("is_first", False),
                )
        elif event["type"] == "native_thinking_signature":
            yield KiroEvent(
                type="thinking_signature",
                thinking_signature=event["data"],
                is_last_thinking_chunk=True,
            )
        elif event["type"] == "tool_use_start":
            yield KiroEvent(
                type="tool_use_start",
                tool_use_id=event["data"]["id"],
                tool_use_name=event["data"]["name"],
                tool_input_delta=event["data"]["fragment"],
            )
        elif event["type"] == "tool_use_delta":
            yield KiroEvent(
                type="tool_use_delta",
                tool_use_id=event["data"]["id"],
                tool_input_delta=event["data"]["fragment"],
            )
        elif event["type"] == "tool_use":
            if event["data"].get("_parse_error"):
                raise MalformedToolInputError("Malformed upstream tool input")
            yield KiroEvent(type="tool_use", tool_use=event["data"])


# ==================================================================================================
# Full Response Collection
# ==================================================================================================


async def collect_stream_to_result(
    response: httpx.Response,
    first_token_timeout: float = FIRST_TOKEN_TIMEOUT,
) -> StreamResult:
    """
    Collects full response from Kiro stream.

    This function consumes the entire stream and returns a StreamResult
    with all accumulated data.

    Args:
        response: HTTP response with stream
        first_token_timeout: First token wait timeout

    Returns:
        StreamResult with full content, thinking, tool calls, and usage
    """
    result = StreamResult()
    full_content_for_bracket_tools = ""
    received_event = False

    async for event in parse_kiro_stream(response, first_token_timeout):
        received_event = True
        if event.type == "content" and event.content:
            result.content += event.content
            full_content_for_bracket_tools += event.content
            if result.content_blocks and result.content_blocks[-1]["type"] == "text":
                result.content_blocks[-1]["text"] += event.content
            else:
                result.content_blocks.append(
                    {
                        "type": "text",
                        "text": event.content,
                    }
                )
        elif event.type == "thinking" and event.thinking_content:
            result.thinking_content += event.thinking_content
            if result.content_blocks and result.content_blocks[-1]["type"] == "thinking":
                result.content_blocks[-1]["thinking"] += event.thinking_content
            else:
                result.content_blocks.append(
                    {
                        "type": "thinking",
                        "thinking": event.thinking_content,
                        "signature": "",
                    }
                )
        elif event.type == "thinking_signature" and event.thinking_signature:
            result.thinking_signature = event.thinking_signature
            for block in reversed(result.content_blocks):
                if block["type"] == "thinking" and not block["signature"]:
                    block["signature"] = event.thinking_signature
                    break
        elif event.type == "tool_use" and event.tool_use:
            result.tool_calls.append(event.tool_use)
            result.content_blocks.append(
                {
                    "type": "tool_use",
                    "tool": event.tool_use,
                }
            )
        elif event.type == "usage" and event.usage:
            result.usage = event.usage
        elif event.type == "context_usage" and event.context_usage_percentage is not None:
            result.context_usage_percentage = event.context_usage_percentage
        elif event.type == "stop_reason" and event.stop_reason:
            result.stop_reason = event.stop_reason

    # Keep this guard at the collector boundary as well: tests and alternate
    # transports may replace the parser with another KiroEvent iterator.
    if not received_event:
        raise StreamProtocolError("Upstream stream ended before any events were received")

    # Recovery is intentionally limited to client-visible text. Reasoning can
    # discuss bracket syntax without actually invoking a tool.
    bracket_tool_calls = parse_bracket_tool_calls(full_content_for_bracket_tools)
    if bracket_tool_calls and result.tool_calls:
        # Upstream already reported this turn's calls as structured events, and the
        # text only echoes them. Keeping both would hand the caller the same call
        # twice, which deduplication cannot always catch because the echoed
        # arguments are re-serialized and no longer compare equal.
        logger.warning(
            "Ignoring {} bracket tool call(s): {} native call(s) already collected",
            len(bracket_tool_calls),
            len(result.tool_calls),
        )
        bracket_tool_calls = []

    if bracket_tool_calls:
        result.tool_calls = deduplicate_tool_calls(result.tool_calls + bracket_tool_calls)
        timeline_tool_ids = {block["tool"].get("id") for block in result.content_blocks if block["type"] == "tool_use"}
        for tool_call in bracket_tool_calls:
            if tool_call.get("id") not in timeline_tool_ids:
                result.content_blocks.append(
                    {
                        "type": "tool_use",
                        "tool": tool_call,
                    }
                )

    # The call is delivered as a tool block, so its text must not remain in the
    # reply as well. Stripping here rather than while accumulating keeps the
    # bracket text available for the detection above.
    if parse_bracket_tool_calls(full_content_for_bracket_tools):
        result.content, _ = split_bracket_call_text(result.content)
        for block in result.content_blocks:
            if block["type"] == "text":
                block["text"], _ = split_bracket_call_text(block["text"])
        result.content_blocks = [block for block in result.content_blocks if block["type"] != "text" or block["text"]]

    return result


# ==================================================================================================
# Token Counting Utilities
# ==================================================================================================


def calculate_tokens_from_context_usage(
    context_usage_percentage: Optional[float], completion_tokens: int, model_cache: "ModelInfoCache", model: str
) -> Tuple[int, int, str, str]:
    """
    Calculate token counts from Kiro's context usage percentage.

    Args:
        context_usage_percentage: Context usage percentage from Kiro API
        completion_tokens: Number of completion tokens (counted via tiktoken)
        model_cache: Model cache for getting max input tokens
        model: Model name

    Returns:
        Tuple of (prompt_tokens, total_tokens, prompt_source, total_source)
    """
    if context_usage_percentage is not None and context_usage_percentage > 0:
        max_input_tokens = model_cache.get_max_input_tokens(model)
        total_tokens = int((context_usage_percentage / 100) * max_input_tokens)
        prompt_tokens = max(0, total_tokens - completion_tokens)
        return prompt_tokens, total_tokens, "subtraction", "API Kiro"

    # Fallback: no context usage data
    return 0, completion_tokens, "unknown", "tiktoken"


# ==================================================================================================
# First Token Retry Logic
# ==================================================================================================


async def stream_with_first_token_retry(
    make_request: Callable[[], Awaitable[httpx.Response]],
    stream_processor: Callable[[httpx.Response], AsyncGenerator[str, None]],
    initial_response: Optional[httpx.Response] = None,
    max_retries: int = FIRST_TOKEN_MAX_RETRIES,
    first_token_timeout: float = FIRST_TOKEN_TIMEOUT,
    on_http_error: Optional[Callable[[int, str], Exception]] = None,
    on_all_retries_failed: Optional[Callable[[int, float], Exception]] = None,
) -> AsyncGenerator[str, None]:
    """
    Generic streaming with automatic retry on first token timeout.

    If model doesn't respond within first_token_timeout seconds,
    request is cancelled and a new one is made. Maximum max_retries attempts.

    This is seamless for user - they just see a delay,
    but eventually get a response (or error after all attempts).

    Args:
        make_request: Function to create new HTTP request (returns httpx.Response)
        stream_processor: Function that processes response and yields SSE strings.
                         Must use parse_kiro_stream internally for timeout handling.
        initial_response: Optional pre-validated response to use on first attempt.
                         If provided, make_request is only called on retries.
                         This allows reusing an already-opened HTTP 200 response.
        max_retries: Maximum number of attempts
        first_token_timeout: First token wait timeout (seconds)
        on_http_error: Optional callback to create exception for HTTP errors.
                      Receives (status_code, error_text), returns Exception.
                      If None, raises generic Exception.
        on_all_retries_failed: Optional callback to create exception when all retries fail.
                              Receives (max_retries, timeout), returns Exception.
                              If None, raises generic Exception.

    Yields:
        Strings in SSE format (format depends on stream_processor)

    Raises:
        Exception from on_http_error or on_all_retries_failed callbacks

    Example:
        >>> async def make_req():
        ...     return await http_client.request_with_retry("POST", url, payload, stream=True)
        >>> async def process(response):
        ...     async for chunk in stream_kiro_to_openai(response, ...):
        ...         yield chunk
        >>> # With initial response (reuse already-validated 200 response)
        >>> response = await make_req()
        >>> async for chunk in stream_with_first_token_retry(make_req, process, initial_response=response):
        ...     print(chunk)
    """
    for attempt in range(max_retries):
        response: Optional[httpx.Response] = None
        try:
            # Make request
            if attempt > 0:
                logger.warning(f"Retry attempt {attempt + 1}/{max_retries} after first token timeout")

            # On first attempt, reuse initial_response if provided
            if attempt == 0 and initial_response is not None:
                response = initial_response
                logger.debug("Reusing initial response for first attempt")
            else:
                response = await make_request()

            if response.status_code != 200:
                # Error from API - close response and raise exception
                try:
                    error_content = await response.aread()
                    error_text = error_content.decode("utf-8", errors="replace")
                except Exception:
                    error_text = "Unknown error"

                try:
                    await response.aclose()
                except Exception:
                    pass

                logger.error(f"Error from Kiro API: {response.status_code} - {error_text}")

                if on_http_error:
                    raise on_http_error(response.status_code, error_text)
                else:
                    raise Exception(f"Upstream API error ({response.status_code}): {error_text}")

            # Try to stream with first token timeout
            async for chunk in stream_processor(response):
                yield chunk

            # Successfully completed - exit
            return

        except FirstTokenTimeoutError:
            logger.warning(
                f"[FirstTokenTimeout] Attempt {attempt + 1}/{max_retries} failed - "
                f"model did not respond within {first_token_timeout}s"
            )

            # Close current response if open
            if response:
                try:
                    await response.aclose()
                except Exception:
                    pass

            # Continue to next attempt
            continue

        except Exception as e:
            # Other errors - no retry, propagate
            # Use positional argument to avoid loguru interpreting curly braces in error message as format placeholders
            # f-string with repr() doesn't work because loguru still sees {type} inside the string
            error_msg = str(e) if str(e) else "(empty message)"
            logger.error("Unexpected error during streaming: {}", error_msg, exc_info=True)
            if response:
                try:
                    await response.aclose()
                except Exception:
                    pass
            raise

    # All attempts exhausted - raise error
    logger.error(
        f"[FirstTokenTimeout] All {max_retries} attempts exhausted - "
        f"model never responded within {first_token_timeout}s per attempt"
    )

    if on_all_retries_failed:
        raise on_all_retries_failed(max_retries, first_token_timeout)
    else:
        raise Exception(
            f"Model did not respond within {first_token_timeout}s after {max_retries} attempts. Please try again."
        )
