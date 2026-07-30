# -*- coding: utf-8 -*-
"""
Kiro Gateway - Proxy for Kiro API.

This package provides a modular architecture for proxying
OpenAI API requests to Kiro (AWS CodeWhisperer).

Modules:
    - config: Configuration and constants
    - models: Pydantic models for OpenAI API
    - auth: Kiro authentication manager
    - cache: Model metadata cache
    - utils: Helper utilities
    - converters: OpenAI <-> Kiro format conversion
    - parsers: AWS SSE stream parsers
    - streaming: Response streaming logic
    - http_client: HTTP client with retry logic
    - routes: FastAPI routes
    - exceptions: Exception handlers
"""

# Version is imported from config.py — the single source of truth
# This allows changing the version in only one place
from kiro.config import APP_VERSION as __version__

__author__ = "Jwadow"

# Main components for convenient import
from kiro.auth import KiroAuthManager
from kiro.cache import ModelInfoCache

# Configuration
from kiro.config import (
    APP_VERSION,
    HIDDEN_MODELS,
    PROXY_API_KEY,
    REGION,
)
from kiro.converters_core import (
    extract_text_content,
    merge_adjacent_messages,
)

# Converters
from kiro.converters_openai import build_kiro_payload

# Exceptions
from kiro.exceptions import (
    sanitize_validation_errors,
    validation_exception_handler,
)
from kiro.http_client import KiroHttpClient
from kiro.model_resolver import ModelResolver, get_model_id_for_kiro, normalize_model_name

# Models
from kiro.models_openai import (
    ChatCompletionRequest,
    ChatMessage,
    ModelList,
    OpenAIModel,
)

# Parsers
from kiro.parsers import (
    AwsEventStreamParser,
    parse_bracket_tool_calls,
)
from kiro.routes_openai import router

# Streaming
from kiro.streaming_openai import (
    collect_stream_response,
    stream_kiro_to_openai,
)

__all__ = [
    # Version
    "__version__",
    # Main classes
    "KiroAuthManager",
    "ModelInfoCache",
    "KiroHttpClient",
    "ModelResolver",
    "router",
    # Configuration
    "PROXY_API_KEY",
    "REGION",
    "HIDDEN_MODELS",
    "APP_VERSION",
    # Model resolution
    "normalize_model_name",
    "get_model_id_for_kiro",
    # Models
    "ChatCompletionRequest",
    "ChatMessage",
    "OpenAIModel",
    "ModelList",
    # Converters
    "build_kiro_payload",
    "extract_text_content",
    "merge_adjacent_messages",
    # Parsers
    "AwsEventStreamParser",
    "parse_bracket_tool_calls",
    # Streaming
    "stream_kiro_to_openai",
    "collect_stream_response",
    # Exceptions
    "validation_exception_handler",
    "sanitize_validation_errors",
]
