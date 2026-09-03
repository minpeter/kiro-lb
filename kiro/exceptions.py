# -*- coding: utf-8 -*-
"""
Exception handlers for Kiro Gateway.

Contains functions for handling validation errors and other exceptions
in a JSON-serialization compatible format.

Data-plane routes (/v1/*) return client-spec error envelopes: the OpenAI
shape ({"error": {message, type, param, code}}) for every /v1/* route
except /v1/messages and /v1/messages/count_tokens, which use the
Anthropic shape ({"type": "error", "error": {type, message}}).
Control-plane routes keep FastAPI's native {"detail": ...} bodies.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger

_ANTHROPIC_EXACT_PATHS = frozenset({"/v1/messages", "/v1/messages/count_tokens"})


def _request_path(request: Request) -> str:
    """Returns the request URL path, tolerating mocked requests in tests."""
    path = getattr(getattr(request, "url", None), "path", "")
    return path if isinstance(path, str) else ""


def _is_anthropic_route(path: str) -> bool:
    """Returns True for /v1/messages and its sub-routes (e.g. count_tokens)."""
    return path in _ANTHROPIC_EXACT_PATHS or path.startswith("/v1/messages/")


def _openai_error_type(status_code: int) -> str:
    """Maps an HTTP status code to an OpenAI-style error type."""
    if status_code == 401:
        return "authentication_error"
    if status_code == 403:
        return "permission_error"
    if status_code == 404:
        return "not_found_error"
    if status_code == 429:
        return "rate_limit_error"
    if 400 <= status_code < 500:
        return "invalid_request_error"
    return "api_error"


def _shape_validation_error(path: str, errors: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Builds the spec-shaped body for a 422 on a data-plane route."""
    first = errors[0] if errors else {}
    message = str(first.get("msg", "Validation error"))
    if _is_anthropic_route(path):
        return {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    loc = first.get("loc", [])
    param = ".".join(str(part) for part in loc) if isinstance(loc, (list, tuple)) else None
    return {
        "error": {
            "message": message,
            "type": "invalid_request_error",
            "param": param,
            "code": None,
        }
    }


def _shape_http_error(path: str, status_code: int, detail: Any) -> dict[str, Any]:
    """Builds the spec-shaped body for an HTTPException on a data-plane route."""
    # Routes may already nest a spec-shaped payload in 'detail'; unwrap it.
    if isinstance(detail, Mapping):
        nested = detail.get("error")
        if _is_anthropic_route(path) and detail.get("type") == "error" and isinstance(nested, Mapping):
            return {"type": "error", "error": dict(nested)}
        if isinstance(nested, Mapping):
            detail = nested
    message = detail if isinstance(detail, str) else str(detail)
    if _is_anthropic_route(path):
        return {
            "type": "error",
            "error": {"type": _openai_error_type(status_code), "message": message},
        }
    return {
        "error": {
            "message": message,
            "type": _openai_error_type(status_code),
            "param": None,
            "code": None,
        }
    }


def sanitize_validation_errors(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """
    Converts validation errors to JSON-serializable format.

    Pydantic may include bytes objects in the 'input' field, which
    are not JSON-serializable. This function converts them to strings.

    Args:
        errors: List of validation errors from Pydantic

    Returns:
        List of errors with bytes converted to strings
    """
    sanitized: list[dict[str, Any]] = []
    for error in errors:
        sanitized_error: dict[str, Any] = {}
        for key, value in error.items():
            if isinstance(value, bytes):
                # Convert bytes to string
                sanitized_error[key] = value.decode("utf-8", errors="replace")
            elif isinstance(value, (list, tuple)):
                # Recursively process lists
                sanitized_error[key] = [
                    v.decode("utf-8", errors="replace") if isinstance(v, bytes) else v for v in value
                ]
            else:
                sanitized_error[key] = value
        sanitized.append(sanitized_error)
    return sanitized


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """
    Pydantic validation error handler.

    Logs error details and returns an informative response.
    Correctly handles bytes objects in errors by converting them to strings.
    Also flushes debug logs for validation errors when DEBUG_MODE is enabled.

    Args:
        request: FastAPI Request object
        exc: Validation exception from Pydantic

    Returns:
        JSONResponse with error details and status 422
    """
    body = await request.body()
    body_str = body.decode("utf-8", errors="replace")

    # Sanitize errors for JSON serialization
    sanitized_errors = sanitize_validation_errors(exc.errors())

    logger.error(f"Validation error (422): {sanitized_errors}")
    # Log body at DEBUG level to avoid cluttering console with potentially large payloads
    # logger.debug(f"Request body: {body_str[:500]}...")

    # Flush debug logs for validation errors
    # This is called AFTER middleware has initialized debug logging,
    # so all app logs during request processing will be captured
    try:
        from kiro.debug_logger import debug_logger

        if debug_logger:
            error_message = f"Validation error: {sanitized_errors}"
            debug_logger.flush_on_error(422, error_message)
    except ImportError:
        pass  # debug_logger not available

    path = _request_path(request)
    if path.startswith("/v1/"):
        content: dict[str, Any] = _shape_validation_error(path, sanitized_errors)
    else:
        content = {"detail": sanitized_errors, "body": body_str[:500]}
    return JSONResponse(status_code=422, content=content)


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """
    HTTPException handler that spec-shapes data-plane error bodies.

    Only /v1/* routes are reshaped: OpenAI clients parse
    error.message and Anthropic clients parse the top-level error
    envelope, while the dashboard UI and deploy tooling depend on
    FastAPI's native {"detail": ...} body for control-plane routes.

    Args:
        request: FastAPI Request object
        exc: The raised HTTPException

    Returns:
        JSONResponse preserving the original status code
    """
    path = _request_path(request)
    if not path.startswith("/v1/"):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None),
        )
    return JSONResponse(
        status_code=exc.status_code,
        content=_shape_http_error(path, exc.status_code, exc.detail),
        headers=getattr(exc, "headers", None),
    )
