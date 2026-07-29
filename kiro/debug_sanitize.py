"""Secret and content redaction for replayable debug artifacts."""

import json
import re
from typing import Any, Optional


_SENSITIVE_KEYS = frozenset({
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api_key",
    "access_token",
    "refresh_token",
    "id_token",
    "client_secret",
    "cookie",
    "set-cookie",
    "signature",
    "profilearn",
})
_STRUCTURAL_TEXT_KEYS = frozenset({
    "type",
    "role",
    "model",
    "name",
    "id",
    "tool_use_id",
    "tool_call_id",
    "stop_reason",
})
_TOKEN_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bklb_[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bapik_[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
)


def redact_patterns(value: str) -> str:
    sanitized = value
    for pattern in _TOKEN_PATTERNS:
        sanitized = pattern.sub("[REDACTED_TOKEN]", sanitized)
    return sanitized


def sanitize_value(
    value: Any,
    capture_content: bool,
    key: Optional[str] = None,
) -> Any:
    """Recursively sanitize credentials and optionally redact textual content."""
    normalized_key = (key or "").lower()
    if normalized_key in _SENSITIVE_KEYS:
        if normalized_key == "signature":
            return "[REDACTED_SIGNATURE]"
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            child_key: sanitize_value(child_value, capture_content, child_key)
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [sanitize_value(item, capture_content, key) for item in value]
    if isinstance(value, str):
        sanitized = redact_patterns(value)
        if capture_content or normalized_key in _STRUCTURAL_TEXT_KEYS:
            return sanitized
        return {"$redacted_text": True, "chars": len(value)}
    return value


def sanitize_bytes(data: bytes, capture_content: bool) -> bytes:
    """Sanitize JSON or SSE bytes while preserving their structural envelope."""
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        return json.dumps({
            "$redacted_bytes": True,
            "bytes": len(data),
        }).encode()
    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError:
        lines = decoded.splitlines()
        if any(line.startswith(("event:", "data:")) for line in lines):
            sanitized_lines = []
            for line in lines:
                if line.startswith("data:"):
                    payload = line.removeprefix("data:").strip()
                    try:
                        parsed_payload = json.loads(payload)
                        payload = json.dumps(
                            sanitize_value(parsed_payload, capture_content),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    except json.JSONDecodeError:
                        payload = json.dumps({
                            "$redacted_text": True,
                            "chars": len(payload),
                        })
                    sanitized_lines.append(f"data: {payload}")
                else:
                    sanitized_lines.append(redact_patterns(line))
            suffix = "\n" if decoded.endswith("\n") else ""
            return ("\n".join(sanitized_lines) + suffix).encode()
        if capture_content:
            return redact_patterns(decoded).encode()
        return json.dumps({
            "$redacted_text": True,
            "chars": len(decoded),
        }).encode()
    return json.dumps(
        sanitize_value(parsed, capture_content),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
