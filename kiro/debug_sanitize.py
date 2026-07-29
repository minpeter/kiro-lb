"""Secret and content redaction for replayable debug artifacts."""

import json
import re
from typing import Any, Optional


_SENSITIVE_KEYS = frozenset({
    "authorization",
    "proxyauthorization",
    "xapikey",
    "apikey",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "clientsecret",
    "cookie",
    "setcookie",
    "signature",
    "thinkingsignature",
    "profilearn",
    "password",
    "token",
    "secret",
    "credential",
    "privatekey",
})
_STRUCTURAL_TEXT_KEYS = frozenset({
    "type",
    "role",
    "model",
    "name",
    "id",
    "tooluseid",
    "toolcallid",
    "stopreason",
    "finishreason",
})
_TOKEN_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bklb_[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bapik_[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
        r"-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(
        r"(?i)([?&](?:key|token|signature|credential)=)[^&#\s]+"
    ),
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
    normalized_key = re.sub(r"[^a-z0-9]", "", (key or "").lower())
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
        if (
            normalized_key == "data"
            and len(value) >= 256
            and re.fullmatch(r"[A-Za-z0-9+/=_-]+", value)
        ):
            return "[REDACTED_BINARY]"
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
        json_start = decoded.find("{")
        if json_start > 0:
            try:
                parsed = json.loads(decoded[json_start:])
            except json.JSONDecodeError:
                pass
            else:
                prefix = redact_patterns(decoded[:json_start])
                sanitized = json.dumps(
                    sanitize_value(parsed, capture_content),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                return (prefix + sanitized).encode()
        lines = decoded.splitlines()
        if any(line.startswith(("event:", "data:")) for line in lines):
            sanitized_lines = []
            for line in lines:
                if line.startswith("data:"):
                    payload = line.removeprefix("data:").strip()
                    if payload == "[DONE]":
                        sanitized_lines.append("data: [DONE]")
                        continue
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
