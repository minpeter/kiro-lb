# -*- coding: utf-8 -*-

"""
Unit tests for streaming_openai module.

Tests for:
- stream_kiro_to_openai() generator
- stream_kiro_to_openai_internal() generator
- stream_with_first_token_retry() function
- collect_stream_response() function
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro.sse_validation import StreamProtocolError
from kiro.streaming_core import KiroEvent
from kiro.streaming_openai import (
    FirstTokenTimeoutError,
    collect_stream_response,
    stream_kiro_to_openai,
    stream_with_first_token_retry,
)

# ==================================================================================================
# Fixtures
# ==================================================================================================


@pytest.fixture
def mock_model_cache():
    """Mock for ModelInfoCache."""
    cache = MagicMock()
    cache.get_max_input_tokens.return_value = 200000
    return cache


@pytest.fixture
def mock_auth_manager():
    """Mock for KiroAuthManager."""
    manager = MagicMock()
    return manager


@pytest.fixture
def mock_http_client():
    """Mock for httpx.AsyncClient."""
    client = AsyncMock()
    return client


@pytest.fixture
def mock_response():
    """Mock for httpx.Response."""
    response = AsyncMock()
    response.status_code = 200
    response.aclose = AsyncMock()
    return response


# ==================================================================================================
# Tests for stream_kiro_to_openai()
# ==================================================================================================


class TestStreamKiroToOpenai:
    """Tests for stream_kiro_to_openai() generator."""

    @pytest.mark.asyncio
    async def test_immediate_eof_has_no_done_marker(
        self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager
    ):
        async def mock_parse_kiro_stream(*args, **kwargs):
            return
            yield  # Make it an async generator.

        chunks = []
        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with pytest.raises(StreamProtocolError, match="before any events"):
                async for chunk in stream_kiro_to_openai(
                    mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                ):
                    chunks.append(chunk)

        assert "data: [DONE]\n\n" not in chunks

    @pytest.mark.asyncio
    async def test_yields_content_chunks(self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager):
        """
        What it does: Yields content chunks in OpenAI format.
        Goal: Verify content streaming.
        """
        print("Setup: Mock stream with content events...")

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(type="content", content="Hello")
            yield KiroEvent(type="content", content=" World")

        print("Action: Streaming to OpenAI format...")
        chunks = []

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
                async for chunk in stream_kiro_to_openai(
                    mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                ):
                    chunks.append(chunk)

        print(f"Received {len(chunks)} chunks")

        # Should have content chunks
        content_chunks = [c for c in chunks if "content" in c and '"Hello"' in c or '" World"' in c]
        assert len(content_chunks) >= 2
        print("✓ Content chunks yielded correctly")

    @pytest.mark.asyncio
    async def test_first_chunk_has_role(self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager):
        """
        What it does: First chunk includes role: assistant.
        Goal: Verify OpenAI streaming protocol.
        """
        print("Setup: Mock stream with content...")

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(type="content", content="Hello")

        print("Action: Streaming to OpenAI format...")
        chunks = []

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
                async for chunk in stream_kiro_to_openai(
                    mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                ):
                    chunks.append(chunk)

        print(f"Received {len(chunks)} chunks")

        first_chunk = json.loads(chunks[0].removeprefix("data: "))
        assert first_chunk["choices"][0]["delta"] == {
            "role": "assistant",
            "content": "",
        }
        print("✓ First chunk has a standalone role: assistant delta")

    @pytest.mark.asyncio
    async def test_yields_done_at_end(self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager):
        """
        What it does: Yields [DONE] at end of stream.
        Goal: Verify stream termination.
        """
        print("Setup: Mock stream with content...")

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(type="content", content="Hello")

        print("Action: Streaming to OpenAI format...")
        chunks = []

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
                async for chunk in stream_kiro_to_openai(
                    mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                ):
                    chunks.append(chunk)

        print(f"Received {len(chunks)} chunks")

        # Last chunk should be [DONE]
        assert chunks[-1] == "data: [DONE]\n\n"
        print("✓ [DONE] yielded at end")

    @pytest.mark.asyncio
    async def test_yields_final_chunk_with_usage(
        self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager
    ):
        """
        What it does: Yields final chunk with usage info.
        Goal: Verify usage is included.
        """
        print("Setup: Mock stream with content...")

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(type="content", content="Hello")
            yield KiroEvent(type="context_usage", context_usage_percentage=5.0)

        print("Action: Streaming to OpenAI format...")
        chunks = []

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
                async for chunk in stream_kiro_to_openai(
                    mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                ):
                    chunks.append(chunk)

        print(f"Received {len(chunks)} chunks")

        # Should have chunk with usage before [DONE]
        usage_chunks = [c for c in chunks if '"usage"' in c]
        assert len(usage_chunks) >= 1
        print("✓ Final chunk with usage yielded")

    @pytest.mark.asyncio
    async def test_yields_tool_calls_chunk(self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager):
        """
        What it does: Yields tool_calls chunk when tools present.
        Goal: Verify tool call streaming.
        """
        print("Setup: Mock stream with tool call...")

        tool_use_data = {
            "id": "call_123",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Moscow"}'},
        }

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(type="content", content="Let me check")
            yield KiroEvent(type="tool_use", tool_use=tool_use_data)

        print("Action: Streaming to OpenAI format...")
        chunks = []

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
                async for chunk in stream_kiro_to_openai(
                    mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                ):
                    chunks.append(chunk)

        print(f"Received {len(chunks)} chunks")

        # Should have tool_calls chunk
        tool_chunks = [c for c in chunks if '"tool_calls"' in c]
        assert len(tool_chunks) >= 1
        assert "get_weather" in tool_chunks[0]
        print("✓ Tool calls chunk yielded")

    @pytest.mark.asyncio
    async def test_tool_request_preserves_reasoning_text_and_parallel_calls(
        self,
        mock_http_client,
        mock_response,
        mock_model_cache,
        mock_auth_manager,
    ):
        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(type="thinking", thinking_content="I will inspect")
            yield KiroEvent(type="content", content="I will search the repository.")
            yield KiroEvent(
                type="tool_use",
                tool_use={
                    "id": "call_search",
                    "type": "function",
                    "function": {
                        "name": "grep",
                        "arguments": '{"pattern":"native tool"}',
                    },
                },
            )
            yield KiroEvent(
                type="tool_use",
                tool_use={
                    "id": "call_bash",
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": '{"command":"pwd"}',
                    },
                },
            )
            yield KiroEvent(type="stop_reason", stop_reason="TOOL_USE")

        chunks = []
        with patch(
            "kiro.streaming_openai.parse_kiro_stream",
            mock_parse_kiro_stream,
        ):
            async for chunk in stream_kiro_to_openai(
                mock_http_client,
                mock_response,
                "claude-opus-5",
                mock_model_cache,
                mock_auth_manager,
                request_tools=[{"type": "function"}],
            ):
                chunks.append(chunk)

        stream = "".join(chunks)
        assert '"reasoning": "I will inspect"' in stream
        assert '"tool_calls"' in stream
        assert "I will search the repository." in stream
        assert "call_search" in stream
        assert "call_bash" in stream
        assert stream.index('"tool_calls"') < stream.index('"finish_reason": "tool_calls"')

    @pytest.mark.asyncio
    async def test_tool_capable_final_text_preserves_reasoning_block(
        self,
        mock_http_client,
        mock_response,
        mock_model_cache,
        mock_auth_manager,
    ):
        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(type="thinking", thinking_content="internal")
            yield KiroEvent(type="content", content="Final answer")
            yield KiroEvent(type="stop_reason", stop_reason="END_TURN")
            yield KiroEvent(type="usage", usage={"credits_used": 0})

        chunks = []
        with patch(
            "kiro.streaming_openai.parse_kiro_stream",
            mock_parse_kiro_stream,
        ):
            async for chunk in stream_kiro_to_openai(
                mock_http_client,
                mock_response,
                "claude-opus-5",
                mock_model_cache,
                mock_auth_manager,
                request_tools=[{"type": "function"}],
            ):
                chunks.append(chunk)

        stream = "".join(chunks)
        assert '"reasoning": "internal"' in stream
        assert "Final answer" in stream
        assert '"finish_reason": "stop"' in stream

    @pytest.mark.asyncio
    async def test_include_reasoning_false_suppresses_reasoning_delta(
        self,
        mock_http_client,
        mock_response,
        mock_model_cache,
        mock_auth_manager,
    ):
        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(type="thinking", thinking_content="internal")
            yield KiroEvent(type="content", content="Final answer")
            yield KiroEvent(type="usage", usage={"credits_used": 0})

        chunks = []
        with patch(
            "kiro.streaming_openai.parse_kiro_stream",
            mock_parse_kiro_stream,
        ):
            async for chunk in stream_kiro_to_openai(
                mock_http_client,
                mock_response,
                "claude-opus-5",
                mock_model_cache,
                mock_auth_manager,
                include_reasoning=False,
            ):
                chunks.append(chunk)

        stream = "".join(chunks)
        assert '"reasoning"' not in stream
        assert "internal" not in stream
        assert "Final answer" in stream

    @pytest.mark.asyncio
    async def test_tool_calls_have_index(self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager):
        """
        What it does: Tool calls have index field.
        Goal: Verify OpenAI streaming spec compliance.
        """
        print("Setup: Mock stream with multiple tool calls...")

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(
                type="tool_use",
                tool_use={"id": "call_1", "type": "function", "function": {"name": "func1", "arguments": "{}"}},
            )
            yield KiroEvent(
                type="tool_use",
                tool_use={"id": "call_2", "type": "function", "function": {"name": "func2", "arguments": "{}"}},
            )

        print("Action: Streaming to OpenAI format...")
        chunks = []

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
                async for chunk in stream_kiro_to_openai(
                    mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                ):
                    chunks.append(chunk)

        print(f"Received {len(chunks)} chunks")

        # Find tool_calls chunk and verify indices
        tool_chunks = [c for c in chunks if '"tool_calls"' in c]
        assert len(tool_chunks) >= 1

        # Parse and check indices
        for chunk in tool_chunks:
            if chunk.startswith("data: "):
                json_str = chunk[6:].strip()
                if json_str != "[DONE]":
                    data = json.loads(json_str)
                    if "choices" in data and data["choices"]:
                        delta = data["choices"][0].get("delta", {})
                        if "tool_calls" in delta:
                            for tc in delta["tool_calls"]:
                                assert "index" in tc

        print("✓ Tool calls have index field")

    @pytest.mark.asyncio
    async def test_parallel_tool_calls_false_returns_only_first_call(
        self,
        mock_http_client,
        mock_response,
        mock_model_cache,
        mock_auth_manager,
    ):
        async def mock_parse_kiro_stream(*args, **kwargs):
            for index in range(2):
                yield KiroEvent(
                    type="tool_use",
                    tool_use={
                        "id": f"call_{index}",
                        "type": "function",
                        "function": {
                            "name": f"tool_{index}",
                            "arguments": "{}",
                        },
                    },
                )
            yield KiroEvent(type="stop_reason", stop_reason="TOOL_USE")

        chunks = []
        with patch(
            "kiro.streaming_openai.parse_kiro_stream",
            mock_parse_kiro_stream,
        ):
            async for chunk in stream_kiro_to_openai(
                mock_http_client,
                mock_response,
                "claude-opus-5",
                mock_model_cache,
                mock_auth_manager,
                parallel_tool_calls=False,
            ):
                chunks.append(chunk)

        stream = "".join(chunks)
        assert "call_0" in stream
        assert "call_1" not in stream

    @pytest.mark.asyncio
    async def test_finish_reason_is_tool_calls_when_tools_present(
        self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager
    ):
        """
        What it does: Sets finish_reason to tool_calls when tools present.
        Goal: Verify correct finish reason.
        """
        print("Setup: Mock stream with tool call...")

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(
                type="tool_use",
                tool_use={"id": "call_1", "type": "function", "function": {"name": "func1", "arguments": "{}"}},
            )

        print("Action: Streaming to OpenAI format...")
        chunks = []

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
                async for chunk in stream_kiro_to_openai(
                    mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                ):
                    chunks.append(chunk)

        print(f"Received {len(chunks)} chunks")

        # Final chunk before [DONE] should have finish_reason: tool_calls
        final_chunk = chunks[-2]  # Before [DONE]
        assert '"finish_reason": "tool_calls"' in final_chunk
        print("✓ finish_reason is tool_calls")

    @pytest.mark.asyncio
    async def test_finish_reason_is_stop_without_tools(
        self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager
    ):
        """
        What it does: Sets finish_reason to stop without tools.
        Goal: Verify correct finish reason.
        """
        print("Setup: Mock stream without tool calls...")

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(type="content", content="Hello")
            yield KiroEvent(type="usage", usage={"inputTokenCount": 10, "outputTokenCount": 1})

        print("Action: Streaming to OpenAI format...")
        chunks = []

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
                async for chunk in stream_kiro_to_openai(
                    mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                ):
                    chunks.append(chunk)

        print(f"Received {len(chunks)} chunks")

        # Final chunk before [DONE] should have finish_reason: stop
        final_chunk = chunks[-2]  # Before [DONE]
        assert '"finish_reason": "stop"' in final_chunk
        print("✓ finish_reason is stop")

    @pytest.mark.asyncio
    async def test_closes_response_on_completion(
        self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager
    ):
        """
        What it does: Closes response on completion.
        Goal: Verify resource cleanup.
        """
        print("Setup: Mock stream...")

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(type="content", content="Hello")

        print("Action: Streaming to OpenAI format...")

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
                async for chunk in stream_kiro_to_openai(
                    mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                ):
                    pass

        print("Check: response.aclose() should be called...")
        mock_response.aclose.assert_called()
        print("✓ Response closed on completion")

    @pytest.mark.asyncio
    async def test_closes_response_on_error(self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager):
        """
        What it does: Closes response on error.
        Goal: Verify resource cleanup on error.
        """
        print("Setup: Mock stream that raises error...")

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(type="content", content="Hello")
            raise RuntimeError("Test error")

        print("Action: Streaming to OpenAI format with error...")

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
                try:
                    async for chunk in stream_kiro_to_openai(
                        mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                    ):
                        pass
                except RuntimeError:
                    pass

        print("Check: response.aclose() should be called...")
        mock_response.aclose.assert_called()
        print("✓ Response closed on error")

    @pytest.mark.asyncio
    async def test_bracket_echo_of_an_intercepted_web_search_is_not_re_emitted(
        self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager
    ):
        """An intercepted web_search is answered locally and never joins the stream list.

        The deduplication reads native signatures from `tool_calls_from_stream`,
        and the interception path `continue`s before appending to it, so an echo
        of the very call that was just answered looked unseen and became a real
        `tool_calls` delta the client executes itself - the same search twice.
        """

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(
                type="tool_use",
                tool_use={
                    "id": "toolu_ws",
                    "function": {"name": "web_search", "arguments": '{"query": "kiro news"}'},
                },
            )
            yield KiroEvent(type="content", content='[Called web_search with args: {"query": "kiro news"}]')
            yield KiroEvent(type="context_usage", context_usage_percentage=4.0)

        echo = [
            {
                "id": "call_bracket_ws",
                "_bracket": True,
                "function": {"name": "web_search", "arguments": '{"query": "kiro news"}'},
            }
        ]

        chunks: list[str] = []
        with (
            patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream),
            patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=echo),
            patch(
                "kiro.mcp_tools.call_kiro_mcp_api",
                AsyncMock(return_value=("srvtoolu_1", {"results": [{"title": "t", "url": "u", "snippet": "s"}]})),
            ),
        ):
            async for chunk in stream_kiro_to_openai(
                mock_http_client, mock_response, "claude-opus-5", mock_model_cache, mock_auth_manager
            ):
                chunks.append(chunk)

        tool_deltas = [
            line for line in chunks if line.startswith("data: ") and "tool_calls" in line and "[DONE]" not in line
        ]
        assert tool_deltas == [], tool_deltas

    @pytest.mark.asyncio
    async def test_bracket_call_absent_from_the_native_channel_is_still_recovered(
        self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager
    ):
        """Suppression is limited to the echo, never to a genuinely different call."""

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(
                type="tool_use",
                tool_use={"id": "toolu_native", "function": {"name": "Edit", "arguments": '{"file_path": "a.py"}'}},
            )
            yield KiroEvent(type="content", content='[Called Bash with args: {"command": "ls"}]')
            yield KiroEvent(type="context_usage", context_usage_percentage=4.0)

        bracket = [
            {
                "id": "call_bracket_bash",
                "_bracket": True,
                "function": {"name": "Bash", "arguments": '{"command": "ls"}'},
            }
        ]

        chunks: list[str] = []
        with (
            patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream),
            patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=bracket),
        ):
            async for chunk in stream_kiro_to_openai(
                mock_http_client, mock_response, "claude-opus-5", mock_model_cache, mock_auth_manager
            ):
                chunks.append(chunk)

        names = [json.loads(line[6:]) for line in chunks if line.startswith("data: ") and "[DONE]" not in line]
        call_names = [
            call["function"]["name"]
            for event in names
            for choice in event.get("choices", [])
            for call in choice.get("delta", {}).get("tool_calls", [])
        ]
        assert "Edit" in call_names
        assert "Bash" in call_names


# ==================================================================================================
# Tests for thinking content handling
# ==================================================================================================


class TestStreamingOpenaiNoneProtection:
    """Tests for None protection in tool calls."""

    @pytest.mark.asyncio
    async def test_handles_none_function_name(
        self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager
    ):
        """
        What it does: Handles None in function.name.
        Goal: Verify None is replaced with empty string.
        """
        print("Setup: Mock stream with None function name...")

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(
                type="tool_use",
                tool_use={"id": "call_1", "type": "function", "function": {"name": None, "arguments": "{}"}},
            )

        print("Action: Streaming to OpenAI format...")
        chunks = []

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
                async for chunk in stream_kiro_to_openai(
                    mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                ):
                    chunks.append(chunk)

        print(f"Received {len(chunks)} chunks")

        # Should handle None gracefully
        tool_chunks = [c for c in chunks if '"tool_calls"' in c]
        assert len(tool_chunks) >= 1

        # Parse and verify name is empty string
        for chunk in tool_chunks:
            if chunk.startswith("data: "):
                json_str = chunk[6:].strip()
                if json_str != "[DONE]":
                    data = json.loads(json_str)
                    if "choices" in data and data["choices"]:
                        delta = data["choices"][0].get("delta", {})
                        if "tool_calls" in delta:
                            for tc in delta["tool_calls"]:
                                assert tc["function"]["name"] == ""

        print("✓ None function name handled")

    @pytest.mark.asyncio
    async def test_handles_none_function_arguments(
        self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager
    ):
        """
        What it does: Handles None in function.arguments.
        Goal: Verify None is replaced with "{}".
        """
        print("Setup: Mock stream with None arguments...")

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(
                type="tool_use",
                tool_use={"id": "call_1", "type": "function", "function": {"name": "func1", "arguments": None}},
            )

        print("Action: Streaming to OpenAI format...")
        chunks = []

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
                async for chunk in stream_kiro_to_openai(
                    mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                ):
                    chunks.append(chunk)

        print(f"Received {len(chunks)} chunks")

        # Should handle None gracefully
        tool_chunks = [c for c in chunks if '"tool_calls"' in c]
        assert len(tool_chunks) >= 1

        # Parse and verify arguments is "{}"
        for chunk in tool_chunks:
            if chunk.startswith("data: "):
                json_str = chunk[6:].strip()
                if json_str != "[DONE]":
                    data = json.loads(json_str)
                    if "choices" in data and data["choices"]:
                        delta = data["choices"][0].get("delta", {})
                        if "tool_calls" in delta:
                            for tc in delta["tool_calls"]:
                                assert tc["function"]["arguments"] == "{}"

        print("✓ None function arguments handled")

    @pytest.mark.asyncio
    async def test_handles_none_function_object(
        self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager
    ):
        """
        What it does: Handles None function object.
        Goal: Verify None function is handled.
        """
        print("Setup: Mock stream with None function...")

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(type="tool_use", tool_use={"id": "call_1", "type": "function", "function": None})

        print("Action: Streaming to OpenAI format...")
        chunks = []

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
                async for chunk in stream_kiro_to_openai(
                    mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                ):
                    chunks.append(chunk)

        print(f"Received {len(chunks)} chunks")

        # Should handle None gracefully without error
        assert len(chunks) > 0
        print("✓ None function object handled")


# ==================================================================================================
# Tests for stream_with_first_token_retry()
# ==================================================================================================


class TestStreamWithFirstTokenRetry:
    """Tests for stream_with_first_token_retry() function."""

    @pytest.mark.asyncio
    async def test_retries_on_first_token_timeout(self, mock_http_client, mock_model_cache, mock_auth_manager):
        """
        What it does: Retries on first token timeout.
        Goal: Verify retry logic.
        """
        print("Setup: Mock make_request that succeeds on second attempt...")

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.aclose = AsyncMock()

        call_count = 0

        async def mock_make_request():
            nonlocal call_count
            call_count += 1
            print(f"make_request called (attempt {call_count})")
            return mock_response

        # First call raises timeout, second succeeds
        timeout_raised = False

        async def mock_parse_kiro_stream_with_retry(*args, **kwargs):
            nonlocal timeout_raised
            if not timeout_raised:
                timeout_raised = True
                raise FirstTokenTimeoutError("Timeout!")
            yield KiroEvent(type="content", content="Success")

        print("Action: Running stream_with_first_token_retry...")
        chunks = []

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream_with_retry):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
                async for chunk in stream_with_first_token_retry(
                    mock_make_request,
                    mock_http_client,
                    "claude-sonnet-4",
                    mock_model_cache,
                    mock_auth_manager,
                    max_retries=3,
                    first_token_timeout=15,
                ):
                    chunks.append(chunk)

        print(f"Received {len(chunks)} chunks")
        print(f"make_request was called {call_count} times")

        assert call_count == 2
        assert len(chunks) > 0
        print("✓ Retry logic worked correctly")

    @pytest.mark.asyncio
    async def test_raises_504_after_all_retries_exhausted(self, mock_http_client, mock_model_cache, mock_auth_manager):
        """
        What it does: Raises 504 after all retries exhausted.
        Goal: Verify error handling.
        """
        from fastapi import HTTPException

        print("Setup: Mock make_request that always times out...")

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.aclose = AsyncMock()

        call_count = 0

        async def mock_make_request():
            nonlocal call_count
            call_count += 1
            return mock_response

        async def mock_parse_kiro_stream_always_timeout(*args, **kwargs):
            raise FirstTokenTimeoutError("Timeout!")
            yield  # Make it a generator

        max_retries = 3

        print(f"Action: Running stream_with_first_token_retry with max_retries={max_retries}...")

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream_always_timeout):
            with pytest.raises(HTTPException) as exc_info:
                async for chunk in stream_with_first_token_retry(
                    mock_make_request,
                    mock_http_client,
                    "claude-sonnet-4",
                    mock_model_cache,
                    mock_auth_manager,
                    max_retries=max_retries,
                    first_token_timeout=15,
                ):
                    pass

        print(f"Caught HTTPException: {exc_info.value.status_code}")
        print(f"make_request was called {call_count} times")

        assert exc_info.value.status_code == 504
        assert call_count == max_retries
        print("✓ 504 raised after all retries exhausted")

    @pytest.mark.asyncio
    async def test_handles_api_error_response(self, mock_http_client, mock_model_cache, mock_auth_manager):
        """
        What it does: Handles API error response.
        Goal: Verify error response handling.
        """
        from fastapi import HTTPException

        print("Setup: Mock make_request that returns error...")

        mock_response = AsyncMock()
        mock_response.status_code = 500
        # Use simple error text without curly braces to avoid loguru format issues
        mock_response.aread = AsyncMock(return_value=b"Internal server error")
        mock_response.aclose = AsyncMock()

        async def mock_make_request():
            return mock_response

        print("Action: Running stream_with_first_token_retry with error response...")

        with pytest.raises(HTTPException) as exc_info:
            async for chunk in stream_with_first_token_retry(
                mock_make_request,
                mock_http_client,
                "claude-sonnet-4",
                mock_model_cache,
                mock_auth_manager,
                max_retries=3,
                first_token_timeout=15,
            ):
                pass

        print(f"Caught HTTPException: {exc_info.value.status_code}")
        assert exc_info.value.status_code == 500
        print("✓ API error response handled")

    @pytest.mark.asyncio
    async def test_propagates_non_timeout_errors(self, mock_http_client, mock_model_cache, mock_auth_manager):
        """
        What it does: Propagates non-timeout errors without retry.
        Goal: Verify only timeout errors trigger retry.
        """
        print("Setup: Mock make_request that raises RuntimeError...")

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.aclose = AsyncMock()

        call_count = 0

        async def mock_make_request():
            nonlocal call_count
            call_count += 1
            return mock_response

        async def mock_parse_kiro_stream_error(*args, **kwargs):
            raise RuntimeError("Test error")
            yield  # Make it a generator

        print("Action: Running stream_with_first_token_retry with RuntimeError...")

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream_error):
            with pytest.raises(RuntimeError) as exc_info:
                async for chunk in stream_with_first_token_retry(
                    mock_make_request,
                    mock_http_client,
                    "claude-sonnet-4",
                    mock_model_cache,
                    mock_auth_manager,
                    max_retries=3,
                    first_token_timeout=15,
                ):
                    pass

        print(f"Caught RuntimeError: {exc_info.value}")
        print(f"make_request was called {call_count} times")

        # Should only be called once - no retry for non-timeout errors
        assert call_count == 1
        assert "Test error" in str(exc_info.value)
        print("✓ Non-timeout errors propagated without retry")

    @pytest.mark.asyncio
    async def test_closes_response_on_retry(self, mock_http_client, mock_model_cache, mock_auth_manager):
        """
        What it does: Closes response when retrying.
        Goal: Verify resource cleanup on retry.
        """
        print("Setup: Mock responses for retry...")

        mock_response1 = AsyncMock()
        mock_response1.status_code = 200
        mock_response1.aclose = AsyncMock()

        mock_response2 = AsyncMock()
        mock_response2.status_code = 200
        mock_response2.aclose = AsyncMock()

        responses = [mock_response1, mock_response2]
        call_count = 0

        async def mock_make_request():
            nonlocal call_count
            response = responses[call_count]
            call_count += 1
            return response

        # First call raises timeout, second succeeds
        timeout_raised = False

        async def mock_parse_kiro_stream_with_retry(*args, **kwargs):
            nonlocal timeout_raised
            if not timeout_raised:
                timeout_raised = True
                raise FirstTokenTimeoutError("Timeout!")
            yield KiroEvent(type="content", content="Success")

        print("Action: Running stream_with_first_token_retry...")

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream_with_retry):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
                async for chunk in stream_with_first_token_retry(
                    mock_make_request,
                    mock_http_client,
                    "claude-sonnet-4",
                    mock_model_cache,
                    mock_auth_manager,
                    max_retries=3,
                    first_token_timeout=15,
                ):
                    pass

        print("Check: First response should be closed...")
        mock_response1.aclose.assert_called()
        print("✓ Response closed on retry")


# ==================================================================================================
# Tests for collect_stream_response()
# ==================================================================================================


class TestStreamingOpenaiErrorHandling:
    """Tests for error handling in streaming_openai."""

    @pytest.mark.asyncio
    async def test_propagates_first_token_timeout_error(
        self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager
    ):
        """
        What it does: Propagates FirstTokenTimeoutError.
        Goal: Verify timeout error is propagated for retry.
        """
        print("Setup: Mock stream that raises timeout...")

        async def mock_parse_kiro_stream(*args, **kwargs):
            raise FirstTokenTimeoutError("Timeout!")
            yield  # Make it a generator

        print("Action: Streaming to OpenAI format with timeout...")

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with pytest.raises(FirstTokenTimeoutError):
                async for chunk in stream_kiro_to_openai(
                    mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                ):
                    pass

        print("✓ FirstTokenTimeoutError propagated correctly")

    @pytest.mark.asyncio
    async def test_propagates_generator_exit(
        self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager
    ):
        """
        What it does: Propagates GeneratorExit after cleanup.
        Goal: Prevent an incomplete stream from being reported as successful.
        """
        print("Setup: Mock stream that raises GeneratorExit...")

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(type="content", content="Hello")
            raise GeneratorExit()

        print("Action: Streaming to OpenAI format with GeneratorExit...")
        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
                with pytest.raises(GeneratorExit):
                    async for _chunk in stream_kiro_to_openai(
                        mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                    ):
                        pass

        # Response should be closed
        mock_response.aclose.assert_awaited_once()
        print("✓ GeneratorExit propagated after cleanup")

    @pytest.mark.asyncio
    async def test_propagates_other_exceptions(
        self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager
    ):
        """
        What it does: Propagates other exceptions.
        Goal: Verify errors are not swallowed.
        """
        print("Setup: Mock stream that raises RuntimeError...")

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(type="content", content="Hello")
            raise RuntimeError("Test error")

        print("Action: Streaming to OpenAI format with RuntimeError...")

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
                with pytest.raises(RuntimeError) as exc_info:
                    async for chunk in stream_kiro_to_openai(
                        mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                    ):
                        pass

        print(f"Caught exception: {exc_info.value}")
        assert "Test error" in str(exc_info.value)
        print("✓ RuntimeError propagated correctly")

    @pytest.mark.asyncio
    async def test_aclose_error_does_not_mask_original(
        self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager
    ):
        """
        What it does: aclose() error doesn't mask original error.
        Goal: Verify original exception is propagated.
        """
        print("Setup: Mock response with error in aclose()...")

        mock_response.aclose = AsyncMock(side_effect=ConnectionError("Connection lost"))

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(type="content", content="Hello")
            raise RuntimeError("Original error")

        print("Action: Streaming to OpenAI format with error and aclose error...")

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
                with pytest.raises(RuntimeError) as exc_info:
                    async for chunk in stream_kiro_to_openai(
                        mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                    ):
                        pass

        print(f"Caught exception: {exc_info.value}")
        assert "Original error" in str(exc_info.value)
        print("✓ Original error not masked by aclose error")


# ==================================================================================================
# Tests for bracket tool calls
# ==================================================================================================


class TestStreamingOpenaiBracketToolCalls:
    """Tests for bracket-style tool call handling."""

    @pytest.mark.asyncio
    async def test_detects_bracket_tool_calls(
        self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager
    ):
        """
        What it does: Detects bracket-style tool calls in content.
        Goal: Verify bracket tool call detection.
        """
        print("Setup: Mock stream with bracket tool calls...")

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(type="content", content="[tool_call: func1]")

        bracket_tool_calls = [{"id": "call_1", "type": "function", "function": {"name": "func1", "arguments": "{}"}}]

        print("Action: Streaming to OpenAI format...")
        chunks = []

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=bracket_tool_calls):
                async for chunk in stream_kiro_to_openai(
                    mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                ):
                    chunks.append(chunk)

        print(f"Received {len(chunks)} chunks")

        # Should have tool_calls chunk
        tool_chunks = [c for c in chunks if '"tool_calls"' in c]
        assert len(tool_chunks) >= 1
        print("✓ Bracket tool calls detected")

    @pytest.mark.asyncio
    async def test_deduplicates_tool_calls(self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager):
        """
        What it does: Deduplicates tool calls from stream and bracket.
        Goal: Verify deduplication.
        """
        print("Setup: Mock stream with duplicate tool calls...")

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(type="content", content="text")
            yield KiroEvent(
                type="tool_use",
                tool_use={"id": "call_1", "type": "function", "function": {"name": "func1", "arguments": "{}"}},
            )

        # Same tool call from bracket detection
        bracket_tool_calls = [{"id": "call_1", "type": "function", "function": {"name": "func1", "arguments": "{}"}}]

        print("Action: Streaming to OpenAI format...")
        chunks = []

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=bracket_tool_calls):
                with patch("kiro.streaming_openai.deduplicate_tool_calls") as mock_dedup:
                    mock_dedup.return_value = [
                        {"id": "call_1", "type": "function", "function": {"name": "func1", "arguments": "{}"}}
                    ]
                    async for chunk in stream_kiro_to_openai(
                        mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                    ):
                        chunks.append(chunk)

                    # Verify deduplicate was called
                    mock_dedup.assert_called()

        print("✓ Tool calls deduplicated")

    @pytest.mark.asyncio
    async def test_bracket_echo_of_a_native_call_reaches_the_client_once(
        self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager
    ):
        """Real deduplication, not a mocked one: the echo must not become a second call.

        The sibling test above patches ``deduplicate_tool_calls`` and therefore
        asserts only that it was called. This one lets it run, because the defect
        lived inside it: a recovered call carries a locally minted id, so the id
        pass never matched and the name+arguments pass only ever inspected
        id-less entries.
        """

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(
                type="tool_use",
                tool_use={
                    "id": "toolu_native",
                    "type": "function",
                    "function": {"name": "Edit", "arguments": '{"file_path":"a.py"}'},
                },
            )
            yield KiroEvent(type="content", content='[Called Edit with args: {"file_path": "a.py"}]')
            yield KiroEvent(type="context_usage", context_usage_percentage=5.0)

        echo = [
            {
                "id": "call_bracket",
                "type": "function",
                "_bracket": True,
                "function": {"name": "Edit", "arguments": '{"file_path": "a.py"}'},
            }
        ]

        chunks: list[str] = []
        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=echo):
                async for chunk in stream_kiro_to_openai(
                    mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                ):
                    chunks.append(chunk)

        emitted = [
            call
            for chunk in chunks
            if chunk.startswith("data: ") and chunk[len("data: ") :].strip() != "[DONE]"
            for choice in json.loads(chunk[len("data: ") :].strip()).get("choices", [])
            for call in (choice.get("delta") or {}).get("tool_calls") or []
        ]

        assert [call["id"] for call in emitted] == ["toolu_native"]
        # Provenance is internal bookkeeping and must never reach a client.
        assert all("_bracket" not in call for call in emitted)
        assert all("_bracket" not in chunk for chunk in chunks)


# ==================================================================================================
# Tests for metering data
# ==================================================================================================


class TestStreamingOpenaiMeteringData:
    """Tests for metering data handling."""

    @pytest.mark.asyncio
    async def test_includes_credits_used_in_usage(
        self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager
    ):
        """
        What it does: Includes credits_used in usage when metering data present.
        Goal: Verify metering data is included.
        """
        print("Setup: Mock stream with metering data...")

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(type="content", content="Hello")
            yield KiroEvent(type="usage", usage={"credits": 0.001})

        print("Action: Streaming to OpenAI format...")
        chunks = []

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
                async for chunk in stream_kiro_to_openai(
                    mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                ):
                    chunks.append(chunk)

        print(f"Received {len(chunks)} chunks")

        # Final chunk should have credits_used
        final_chunk = chunks[-2]  # Before [DONE]
        assert '"credits_used"' in final_chunk
        print("✓ credits_used included in usage")


# ==================================================================================================
# Tests for truncation detection
# ==================================================================================================


class TestStreamingOpenaiTruncationDetection:
    """Tests for truncation detection in OpenAI streaming."""

    @pytest.mark.asyncio
    async def test_finish_reason_is_length_when_truncated(
        self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager
    ):
        """
        What it does: Sets finish_reason to length when content is truncated.
        Goal: Verify truncation detection without completion signals.
        """
        print("Setup: Mock stream without completion signals (truncated)...")

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(type="content", content="This response was cut off mid-sentence because")
            # No usage event = truncation

        print("Action: Streaming to OpenAI format...")
        chunks = []

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
                async for chunk in stream_kiro_to_openai(
                    mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                ):
                    chunks.append(chunk)

        print(f"Received {len(chunks)} chunks")

        # Final chunk before [DONE] should have finish_reason: length
        final_chunk = chunks[-2]  # Before [DONE]
        print(f"Comparing finish_reason: Expected 'length', Got chunk: {final_chunk}")
        assert '"finish_reason": "length"' in final_chunk
        print("✓ finish_reason is length when truncated")

    @pytest.mark.asyncio
    async def test_finish_reason_is_tool_calls_even_without_completion_signals(
        self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager
    ):
        """
        What it does: Sets finish_reason to tool_calls when tool calls present.
        Goal: Verify tool_calls take priority (not confused with content truncation).
        """
        print("Setup: Mock stream with tool calls but no completion signals...")

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(type="content", content="Let me call a tool")
            yield KiroEvent(
                type="tool_use",
                tool_use={"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}},
            )
            # No usage event, but tool calls present = tool_calls finish_reason

        print("Action: Streaming to OpenAI format...")
        chunks = []

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
                async for chunk in stream_kiro_to_openai(
                    mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                ):
                    chunks.append(chunk)

        print(f"Received {len(chunks)} chunks")

        # Tool calls take priority (not confused with content truncation)
        final_chunk = chunks[-2]  # Before [DONE]
        print(f"Comparing finish_reason: Expected 'tool_calls', Got chunk: {final_chunk}")
        assert '"finish_reason": "tool_calls"' in final_chunk
        print("✓ finish_reason is tool_calls (not confused with content truncation)")

    @pytest.mark.asyncio
    async def test_finish_reason_is_stop_with_completion_signals(
        self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager
    ):
        """
        What it does: Sets finish_reason to stop when completion signals present.
        Goal: Verify normal completion is detected correctly.
        """
        print("Setup: Mock stream with completion signals (not truncated)...")

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(type="content", content="Complete response")
            yield KiroEvent(type="usage", usage={"inputTokenCount": 10, "outputTokenCount": 5})

        print("Action: Streaming to OpenAI format...")
        chunks = []

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
                async for chunk in stream_kiro_to_openai(
                    mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                ):
                    chunks.append(chunk)

        print(f"Received {len(chunks)} chunks")

        # With completion signals, should be stop
        final_chunk = chunks[-2]  # Before [DONE]
        print(f"Comparing finish_reason: Expected 'stop', Got chunk: {final_chunk}")
        assert '"finish_reason": "stop"' in final_chunk
        print("✓ finish_reason is stop with completion signals")

    @pytest.mark.asyncio
    async def test_collect_extracts_finish_reason_from_chunks(
        self, mock_http_client, mock_response, mock_model_cache, mock_auth_manager
    ):
        """
        What it does: Non-streaming extracts finish_reason from streaming chunks.
        Goal: Verify collect_stream_response correctly extracts finish_reason.
        """
        print("Setup: Mock stream without completion signals...")

        async def mock_parse_kiro_stream(*args, **kwargs):
            yield KiroEvent(type="content", content="Truncated")
            # No usage = truncation

        print("Action: Collecting stream response...")

        with patch("kiro.streaming_openai.parse_kiro_stream", mock_parse_kiro_stream):
            with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
                result = await collect_stream_response(
                    mock_http_client, mock_response, "claude-sonnet-4", mock_model_cache, mock_auth_manager
                )

        print(f"Result finish_reason: {result['choices'][0]['finish_reason']}")

        # Should extract "length" from streaming chunks
        assert result["choices"][0]["finish_reason"] == "length"
        print("✓ collect_stream_response extracts finish_reason correctly")
