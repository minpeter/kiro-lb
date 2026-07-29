"""Validate, replay, and export sanitized failed-stream captures."""

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

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
    descriptor = os.open(
        output,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        for record in replay.get("translated_sse", []):
            handle.write(base64.b64decode(record.get("payload_base64", "")))
    output.chmod(0o600)
    validate_replay(replay, protocol)


def export_fixture(replay_path: Path, output: Path) -> None:
    """Export only structurally redacted captures that contain no credentials."""
    replay = load_replay(replay_path)
    if replay.get("capture_content"):
        raise ValueError("Cannot export capture with content enabled")
    if _contains_credential(replay):
        raise ValueError("Capture contains a credential pattern")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(
        json.dumps(replay, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(output)


def _contains_credential(replay: dict[str, Any]) -> bool:
    values = [json.dumps(replay, ensure_ascii=False)]
    for key in ("upstream_chunks", "translated_sse"):
        for record in replay.get(key, []):
            try:
                decoded = base64.b64decode(
                    record.get("payload_base64", ""),
                    validate=True,
                ).decode("utf-8", errors="replace")
            except (ValueError, TypeError):
                continue
            values.append(decoded)
    return any(
        pattern.search(value)
        for value in values
        for pattern in _SECRET_PATTERNS
    )


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
