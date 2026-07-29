"""Validate, replay, and export sanitized failed-stream captures."""

import argparse
import base64
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from kiro.debug_sanitize import redact_patterns, sensitive_key_kind
from kiro.sse_validation import (
    StreamProtocolError,
    validate_anthropic_records,
    validate_openai_records,
)


_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\b(?:klb|apik)_[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\."),
)


def load_replay(path: Path) -> dict[str, Any]:
    """Load a replay document from a bundle directory or JSON path."""
    replay_path = path / "replay.json" if path.is_dir() else path
    return json.loads(replay_path.read_text(encoding="utf-8"))


def validate_replay(replay: dict[str, Any], protocol: str) -> None:
    """Validate the selected translated stream without contacting a provider."""
    if protocol == "anthropic":
        validate_anthropic_records(replay.get("translated_sse", []))
        return
    if protocol == "openai":
        validate_openai_records(replay.get("translated_sse", []))
        return
    raise ValueError(f"Unsupported protocol: {protocol}")


def replay_to_file(
    replay: dict[str, Any],
    output: Path,
    protocol: str,
) -> None:
    """Write the sanitized translated stream and then validate its lifecycle."""
    payload = b"".join(
        base64.b64decode(record.get("payload_base64", ""))
        for record in replay.get("translated_sse", [])
    )
    _write_private_atomic(output, payload)
    validate_replay(replay, protocol)


def export_fixture(replay_path: Path, output: Path) -> None:
    """Export only structurally redacted captures that contain no credentials."""
    replay = load_replay(replay_path)
    if replay.get("capture_content"):
        raise ValueError("Cannot export capture with content enabled")
    decoded_payloads = _decode_capture_payloads(replay)
    if _contains_credential(replay, decoded_payloads):
        raise ValueError("Capture contains a credential pattern")
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_private_atomic(
        output,
        json.dumps(replay, indent=2, ensure_ascii=False).encode("utf-8"),
    )


def _decode_capture_payloads(replay: dict[str, Any]) -> list[str]:
    decoded_payloads = []
    for key in ("upstream_chunks", "translated_sse"):
        records = replay.get(key, [])
        if not isinstance(records, list):
            raise ValueError(f"Capture has invalid {key} records")
        for record in records:
            if not isinstance(record, dict):
                raise ValueError(f"Capture has invalid {key} record")
            encoded = record.get("payload_base64")
            if not isinstance(encoded, str):
                raise ValueError("Capture contains invalid base64 payload")
            try:
                decoded = base64.b64decode(
                    encoded,
                    validate=True,
                ).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError(
                    "Capture contains invalid base64 payload"
                ) from exc
            decoded_payloads.append(decoded)
    return decoded_payloads


def _contains_credential(
    replay: dict[str, Any],
    decoded_payloads: list[str],
) -> bool:
    values = [json.dumps(replay, ensure_ascii=False), *decoded_payloads]
    return any(
        pattern.search(value)
        for value in values
        for pattern in _SECRET_PATTERNS
    ) or any(
        redact_patterns(value) != value
        for value in values
    ) or _contains_unredacted_sensitive_field(replay) or any(
        _text_contains_unredacted_sensitive_field(value)
        for value in decoded_payloads
    )


def _contains_unredacted_sensitive_field(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if sensitive_key_kind(str(key)) is not None and child not in (
                "[REDACTED]",
                "[REDACTED_SIGNATURE]",
            ):
                return True
            if _contains_unredacted_sensitive_field(child):
                return True
    elif isinstance(value, list):
        return any(_contains_unredacted_sensitive_field(item) for item in value)
    elif isinstance(value, str):
        return _text_contains_unredacted_sensitive_field(value)
    return False


def _text_contains_unredacted_sensitive_field(value: str) -> bool:
    candidates = [value]
    candidates.extend(
        line.removeprefix("data:").strip()
        for line in value.splitlines()
        if line.startswith("data:")
    )
    json_start = value.find("{")
    if json_start > 0:
        candidates.append(value[json_start:])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, (dict, list)) and (
            _contains_unredacted_sensitive_field(parsed)
        ):
            return True
    return False


def _write_private_atomic(output: Path, payload: bytes) -> None:
    """Create a private temporary file and atomically replace the output."""
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main(argv: Optional[list[str]] = None) -> int:
    """Run the debug replay command-line interface."""
    parser = argparse.ArgumentParser(prog="python -m kiro.debug_replay")
    commands = parser.add_subparsers(dest="command", required=True)

    validate_parser = commands.add_parser("validate")
    validate_parser.add_argument("capture", type=Path)
    validate_parser.add_argument(
        "--protocol",
        choices=("anthropic", "openai"),
        default="anthropic",
    )

    replay_parser = commands.add_parser("replay")
    replay_parser.add_argument("capture", type=Path)
    replay_parser.add_argument(
        "--protocol",
        choices=("anthropic", "openai"),
        required=True,
    )
    replay_parser.add_argument("--output", type=Path, required=True)

    export_parser = commands.add_parser("export-fixture")
    export_parser.add_argument("capture", type=Path)
    export_parser.add_argument("output", type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            validate_replay(load_replay(args.capture), args.protocol)
        elif args.command == "replay":
            replay_to_file(
                load_replay(args.capture),
                args.output,
                args.protocol,
            )
        else:
            export_fixture(args.capture, args.output)
    except StreamProtocolError:
        print("Invalid assistant content event order", file=sys.stderr)
        return 3
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
