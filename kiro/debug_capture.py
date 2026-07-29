"""Request-scoped, bounded, and secret-safe debug capture persistence."""

import base64
import hashlib
import io
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from kiro.debug_sanitize import redact_patterns, sanitize_bytes


def _bounded(data: bytes, available: int) -> tuple[bytes, bool, int]:
    if available <= 0:
        return b"", bool(data), len(data)
    if len(data) <= available:
        return data, False, 0
    head_size = available // 2
    tail_size = available - head_size
    omitted = len(data) - available
    return data[:head_size] + data[-tail_size:], True, omitted


class CaptureLogBuffer(io.StringIO):
    """Bound application-log memory to the request capture budget."""

    def __init__(self, state: "CaptureState") -> None:
        super().__init__()
        self._state = state

    def write(self, message: str) -> int:
        encoded = message.encode("utf-8", errors="replace")
        stored, truncated, omitted = _bounded(
            encoded,
            self._state._remaining(),
        )
        self._state.stored_bytes += len(stored)
        if stored:
            super().write(
                stored.decode("utf-8", errors="replace")
            )
        metadata = self._state.artifact_meta.setdefault("app_logs", {
            "original_bytes": 0,
            "stored_bytes": 0,
            "truncated": False,
            "omitted_bytes": 0,
        })
        metadata["original_bytes"] += len(encoded)
        metadata["stored_bytes"] += len(stored)
        metadata["truncated"] = metadata["truncated"] or truncated
        metadata["omitted_bytes"] += omitted
        return len(message)

@dataclass
class CaptureState:
    """Mutable request-local evidence collected before atomic publication."""

    debug_dir: Path
    capture_content: bool
    max_bytes: int
    retention: int
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    sequence: int = 0
    stored_bytes: int = 0
    stored_chunk_records: int = 0
    client_request: Any = None
    kiro_request: Any = None
    upstream_chunks: list[dict[str, Any]] = field(default_factory=list)
    translated_sse: list[dict[str, Any]] = field(default_factory=list)
    app_logs: str = ""
    app_log_buffer: CaptureLogBuffer = field(init=False, repr=False)
    loguru_sink_id: Optional[int] = field(default=None, repr=False)
    artifact_meta: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.app_log_buffer = CaptureLogBuffer(self)

    @property
    def data_budget(self) -> int:
        """Reserve room for replay duplication, manifests, and metadata."""
        return max(1024, self.max_bytes // 3)

    def _remaining(self) -> int:
        return max(0, self.data_budget - self.stored_bytes)

    def set_json_artifact(self, name: str, data: bytes) -> None:
        sanitized = sanitize_bytes(data, self.capture_content)
        stored, truncated, omitted = _bounded(sanitized, self._remaining())
        self.stored_bytes += len(stored)
        try:
            value = json.loads(stored)
        except (json.JSONDecodeError, UnicodeDecodeError):
            value = {
                "$redacted_bytes": True,
                "stored_bytes": len(stored),
                "original_bytes": len(data),
            }
        setattr(self, name, value)
        self.artifact_meta[name] = {
            "original_bytes": len(data),
            "stored_bytes": len(stored),
            "truncated": truncated,
            "omitted_bytes": omitted,
        }

    def add_chunk(self, target: str, data: bytes) -> None:
        sanitized = sanitize_bytes(data, self.capture_content)
        stored, truncated, omitted = _bounded(sanitized, self._remaining())
        record_limit = max(1, self.max_bytes // 1024)
        if self.stored_chunk_records >= record_limit:
            stored = b""
            truncated = bool(sanitized)
            omitted = len(sanitized)
        else:
            self.stored_bytes += len(stored)
        record = None
        if stored:
            record = {
                "seq": self.sequence,
                "size": len(data),
                "stored_size": len(stored),
                "truncated": truncated,
                "omitted_bytes": omitted,
                "payload_base64": base64.b64encode(stored).decode(),
            }
            self.stored_chunk_records += 1
        self.sequence += 1
        if record is not None:
            getattr(self, target).append(record)
        metadata = self.artifact_meta.setdefault(target, {
            "original_bytes": 0,
            "stored_bytes": 0,
            "truncated": False,
            "omitted_bytes": 0,
        })
        metadata["original_bytes"] += len(data)
        metadata["stored_bytes"] += len(stored)
        metadata["truncated"] = metadata["truncated"] or truncated
        metadata["omitted_bytes"] += omitted

    def publish(
        self,
        status_code: int,
        error_message: str,
        category: str = "failures",
    ) -> Path:
        """Atomically publish a private failure bundle and enforce retention."""
        prune_stale_temporaries(self.debug_dir)
        captures_dir = self.debug_dir / category
        captures_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(captures_dir, 0o700)
        temporary = captures_dir / f".tmp-{self.request_id}"
        if temporary.exists():
            shutil.rmtree(temporary)
        temporary.mkdir(mode=0o700)

        try:
            failure = redact_patterns(error_message)
            validation = {"valid": True, "failure": None}
            if self.translated_sse:
                from kiro.sse_validation import (
                    StreamProtocolError,
                    validate_anthropic_records,
                    validate_openai_records,
                )

                try:
                    if _translated_protocol(self.translated_sse) == "openai":
                        validate_openai_records(self.translated_sse)
                    else:
                        validate_anthropic_records(self.translated_sse)
                except StreamProtocolError as exc:
                    validation = {"valid": False, "failure": str(exc)}
            replay = {
                "schema_version": 1,
                "request_id": self.request_id,
                "failure": failure,
                "capture_content": self.capture_content,
                "client_request": self.client_request,
                "kiro_request": self.kiro_request,
                "upstream_chunks": self.upstream_chunks,
                "translated_sse": self.translated_sse,
                "validation": validation,
            }
            artifacts: dict[str, bytes] = {
                "client_request.json": _json_bytes(self.client_request),
                "kiro_request.json": _json_bytes(self.kiro_request),
                "upstream_chunks.jsonl": _jsonl_bytes(self.upstream_chunks),
                "translated_sse.jsonl": _jsonl_bytes(self.translated_sse),
                "app_logs.txt": self._sanitized_logs(),
                "replay.json": _json_bytes(replay),
            }
            artifact_manifest: dict[str, dict[str, Any]] = {}
            for name, payload in artifacts.items():
                _write_private_file(temporary / name, payload)
                source_key = name.removesuffix(".json").removesuffix(".jsonl")
                metadata = self.artifact_meta.get(source_key, {})
                artifact_manifest[name] = {
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "bytes": len(payload),
                    "truncated": bool(metadata.get("truncated", False)),
                    "original_bytes": metadata.get("original_bytes", len(payload)),
                }
            manifest = {
                "schema_version": 1,
                "request_id": self.request_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status_code": status_code,
                "failure": failure,
                "capture_content": self.capture_content,
                "artifacts": artifact_manifest,
            }
            _write_private_file(temporary / "manifest.json", _json_bytes(manifest))
            _fsync_directory(temporary)

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            final_path = captures_dir / f"{timestamp}-{self.request_id}"
            os.replace(temporary, final_path)
            os.chmod(final_path, 0o700)
            _fsync_directory(captures_dir)
            _prune_completed(captures_dir, self.retention)
            return final_path
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def _sanitized_logs(self) -> bytes:
        if not self.app_logs:
            return b""
        if self.capture_content:
            return redact_patterns(self.app_logs).encode()
        return f"[REDACTED_LOGS chars={len(self.app_logs)}]\n".encode()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, indent=2, ensure_ascii=False).encode()


def _translated_protocol(records: list[dict[str, Any]]) -> str:
    for record in records:
        payload = base64.b64decode(record.get("payload_base64", ""))
        stripped = payload.lstrip()
        if stripped.startswith(b"data:"):
            return "openai"
        if stripped.startswith(b"event:"):
            return "anthropic"
    return "anthropic"


def _jsonl_bytes(records: list[dict[str, Any]]) -> bytes:
    return b"".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode()
        + b"\n"
        for record in records
    )


def _write_private_file(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if not os.path.exists(path):
            os.close(descriptor)
    os.chmod(path, 0o600)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _prune_completed(failures_dir: Path, retention: int) -> None:
    completed = sorted(
        path
        for path in failures_dir.iterdir()
        if path.is_dir() and not path.name.startswith(".tmp-")
    )
    for stale in completed[:-retention]:
        shutil.rmtree(stale)


def prune_stale_temporaries(
    debug_dir: Path,
    max_age_seconds: int = 24 * 60 * 60,
) -> None:
    """Remove abandoned atomic-write directories without touching live ones."""
    cutoff = time.time() - max_age_seconds
    for category in ("failures", "requests"):
        captures_dir = debug_dir / category
        if not captures_dir.is_dir():
            continue
        for temporary in captures_dir.glob(".tmp-*"):
            try:
                if temporary.stat().st_mtime < cutoff:
                    shutil.rmtree(temporary)
            except FileNotFoundError:
                continue
