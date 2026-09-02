# -*- coding: utf-8 -*-
"""
Pydantic models for OpenAI-compatible API.

Defines data schemas for requests and responses,
providing validation and serialization.
"""

import time
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field
from typing_extensions import Annotated

# ==================================================================================================
# Models for /v1/models endpoint
# ==================================================================================================


class OpenAIModel(BaseModel):
    """
    One model entry, carrying both the OpenAI and the Anthropic field sets.

    Claude Code 2.1.126+ discovers gateway models through GET /v1/models and only
    parses the Anthropic-native shape (type / display_name / created_at), while
    OpenAI clients read object / created / owned_by. Both routers mount on the
    same app, so a second registration of the same path would be shadowed; one
    superset response serves both SDKs instead, and each ignores the other's
    unknown fields.

    context_window and max_input_tokens are vendor extensions. They exist because
    the gateway already resolves each real limit, and without them every client
    has to hardcode a window that the upstream sometimes over-reports.
    """

    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "anthropic"
    description: Optional[str] = None

    type: str = "model"
    display_name: Optional[str] = None
    created_at: Optional[str] = None

    context_window: Optional[int] = None
    max_input_tokens: Optional[int] = None
    max_tokens: Optional[int] = None


class ModelList(BaseModel):
    """
    Model list response for GET /v1/models.

    object/data satisfy the OpenAI list wrapper; has_more/first_id/last_id
    satisfy the Anthropic page wrapper.
    """

    object: str = "list"
    data: List[OpenAIModel]
    has_more: bool = False
    first_id: Optional[str] = None
    last_id: Optional[str] = None


# ==================================================================================================
# Models for /v1/chat/completions endpoint
# ==================================================================================================


class ChatMessage(BaseModel):
    """
    Chat message in OpenAI format.

    Supports various roles (user, assistant, system, tool)
    and various content formats (string, list, object).

    Attributes:
        role: Sender role (user, assistant, system, tool)
        content: Message content (can be string, list, or None)
        name: Optional sender name
        tool_calls: List of tool calls (for assistant)
        tool_call_id: Tool call ID (for tool)
        is_error: Whether a tool message reports a failure. Not part of OpenAI's
            schema, but Kiro's toolResult carries a status and the Anthropic
            protocol does define this flag, so a client that sends it is honoured.
            Declared rather than read off the extra-allow bag so pydantic
            validates it: `bool("false")` is True, which would have reported a
            successful tool as failed.
    """

    role: str
    content: Optional[Union[str, List[Any], Any]] = None
    name: Optional[str] = None
    tool_calls: Optional[List[Any]] = None
    tool_call_id: Optional[str] = None
    reasoning: Optional[str] = None
    reasoning_content: Optional[str] = None
    is_error: Optional[bool] = None

    model_config = {"extra": "allow"}


class ToolFunction(BaseModel):
    """
    Tool function description.

    Attributes:
        name: Function name
        description: Function description
        parameters: JSON Schema of function parameters
    """

    name: str
    description: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None


class Tool(BaseModel):
    """
    Tool in OpenAI format.

    Supports two formats:
    1. Standard OpenAI format: {"type": "function", "function": {...}}
    2. Flat format (Cursor-style): {"name": "...", "description": "...", "input_schema": {...}}

    Attributes:
        type: Tool type (usually "function")
        function: Function description (standard format)
        name: Function name (flat format)
        description: Function description (flat format)
        input_schema: Function parameters (flat format)
    """

    # Standard OpenAI format fields
    type: str = "function"
    function: Optional[ToolFunction] = None

    # Flat format fields (Cursor-style)
    name: Optional[str] = None
    description: Optional[str] = None
    input_schema: Optional[Dict[str, Any]] = None

    model_config = {"extra": "allow"}


class ChatCompletionRequest(BaseModel):
    """
    Request for response generation in OpenAI Chat Completions API format.

    Supports all standard OpenAI API fields, including:
    - Basic parameters (model, messages, stream)
    - Generation parameters (temperature, top_p, max_tokens)
    - Tools (function calling)
    - Additional parameters (ignored but accepted for compatibility)

    Attributes:
        model: Model ID for generation
        messages: List of chat messages
        stream: Use streaming (default False)
        temperature: Generation temperature (0-2)
        top_p: Top-p sampling
        n: Number of response variants
        max_tokens: Maximum number of tokens in response
        max_completion_tokens: Alternative field for max_tokens
        stop: Stop sequences
        presence_penalty: Penalty for topic repetition
        frequency_penalty: Penalty for word repetition
        tools: List of available tools
        tool_choice: Tool selection strategy
    """

    model: str
    messages: Annotated[List[ChatMessage], Field(min_length=1)]
    stream: bool = False

    # Generation parameters
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    n: Optional[int] = 1
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    stop: Optional[Union[str, List[str]]] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None

    # Reasoning (OpenAI reasoning models)
    # Supports all official reasoning_effort levels from OpenAI API
    reasoning_effort: Optional[Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]] = None
    include_reasoning: bool = True

    # Tools (function calling)
    tools: Optional[List[Tool]] = None
    tool_choice: Optional[Union[str, Dict]] = None

    # Compatibility fields (ignored)
    stream_options: Optional[Dict[str, Any]] = None
    logit_bias: Optional[Dict[str, float]] = None
    logprobs: Optional[bool] = None
    top_logprobs: Optional[int] = None
    user: Optional[str] = None
    seed: Optional[int] = None
    parallel_tool_calls: Optional[bool] = None

    model_config = {"extra": "allow"}


# ==================================================================================================
# Models for responses
# ==================================================================================================


class ChatCompletionChoice(BaseModel):
    """
    Single response variant in Chat Completion.

    Attributes:
        index: Variant index
        message: Response message
        finish_reason: Completion reason (stop, tool_calls, length)
    """

    index: int = 0
    message: Dict[str, Any]
    finish_reason: Optional[str] = None


class ChatCompletionUsage(BaseModel):
    """
    Token usage information.

    Attributes:
        prompt_tokens: Number of tokens in request
        completion_tokens: Number of tokens in response
        total_tokens: Total number of tokens
        credits_used: Credits used (Kiro-specific)
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    credits_used: Optional[float] = None


class ChatCompletionResponse(BaseModel):
    """
    Full Chat Completion response (non-streaming).

    Attributes:
        id: Unique response ID
        object: Object type ("chat.completion")
        created: Creation timestamp
        model: Model used
        choices: List of response variants
        usage: Token usage information
    """

    id: str
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatCompletionChoice]
    usage: ChatCompletionUsage


class ChatCompletionChunkDelta(BaseModel):
    """
    Delta of changes in streaming chunk.

    Attributes:
        role: Role (only in first chunk)
        content: New content
        tool_calls: New tool calls
    """

    role: Optional[str] = None
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None


class ChatCompletionChunkChoice(BaseModel):
    """
    Single variant in streaming chunk.

    Attributes:
        index: Variant index
        delta: Delta of changes
        finish_reason: Completion reason (only in last chunk)
    """

    index: int = 0
    delta: ChatCompletionChunkDelta
    finish_reason: Optional[str] = None


class ChatCompletionChunk(BaseModel):
    """
    Streaming chunk in OpenAI format.

    Attributes:
        id: Unique response ID
        object: Object type ("chat.completion.chunk")
        created: Creation timestamp
        model: Model used
        choices: List of variants
        usage: Usage information (only in last chunk)
    """

    id: str
    object: str = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatCompletionChunkChoice]
    usage: Optional[ChatCompletionUsage] = None
