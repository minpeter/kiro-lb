"""Stateful validation for translated OpenAI and Anthropic stream ordering."""

import base64
import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Optional


class StreamProtocolError(RuntimeError):
    """Protocol-order failure safe to expose without payload contents."""


@dataclass
class AnthropicSSEValidator:
    """Validate one Anthropic Messages SSE lifecycle."""

    message_started: bool = False
    active_index: Optional[int] = None
    active_type: Optional[str] = None
    last_index: int = -1
    message_delta_seen: bool = False
    message_stopped: bool = False
    signatures: set[int] = field(default_factory=set)

    def accept(self, event_type: str, data: dict[str, Any]) -> None:
        """Accept one event or raise the stable client-facing order error."""
        if self.message_stopped:
            self._fail()
        if event_type == "message_start":
            if self.message_started:
                self._fail()
            self.message_started = True
            return
        if not self.message_started:
            self._fail()
        if event_type == "content_block_start":
            index = data.get("index")
            if (
                self.active_index is not None
                or not isinstance(index, int)
                or index != self.last_index + 1
            ):
                self._fail()
            assert isinstance(index, int)
            self.active_index = index
            self.active_type = (data.get("content_block") or {}).get("type")
            self.last_index = index
            return
        if event_type == "content_block_delta":
            if data.get("index") != self.active_index:
                self._fail()
            delta_type = (data.get("delta") or {}).get("type")
            if delta_type == "signature_delta" and self.active_index is not None:
                if self.active_type != "thinking":
                    self._fail()
                self.signatures.add(self.active_index)
            return
        if event_type == "content_block_stop":
            if data.get("index") != self.active_index:
                self._fail()
            self.active_index = None
            self.active_type = None
            return
        if event_type == "message_delta":
            if self.active_index is not None or self.message_delta_seen:
                self._fail()
            self.message_delta_seen = True
            return
        if event_type == "message_stop":
            if self.active_index is not None or not self.message_delta_seen:
                self._fail()
            self.message_stopped = True
            return
        if event_type in {"ping", "error"}:
            return

    def finish(self) -> None:
        """Require a complete terminal lifecycle."""
        if not self.message_stopped:
            self._fail()

    @staticmethod
    def _fail() -> None:
        raise StreamProtocolError("Invalid assistant content event order")


@dataclass
class OpenAIStreamValidator:
    """Validate terminal ordering for one OpenAI chat-completion stream."""

    terminal_seen: bool = False
    done_seen: bool = False
    tool_indices: set[int] = field(default_factory=set)

    def accept(self, payload: Optional[dict[str, Any]], done: bool = False) -> None:
        """Accept one OpenAI SSE payload or reject events after termination."""
        if self.done_seen:
            raise StreamProtocolError("Invalid assistant content event order")
        if done:
            if not self.terminal_seen:
                raise StreamProtocolError("Invalid assistant content event order")
            self.done_seen = True
            return
        if payload is None:
            return
        choices = payload.get("choices") or []
        for choice in choices:
            delta = choice.get("delta") or {}
            for tool_call in delta.get("tool_calls") or []:
                index = tool_call.get("index")
                if not isinstance(index, int):
                    raise StreamProtocolError(
                        "Invalid assistant content event order"
                    )
                self.tool_indices.add(index)
            if choice.get("finish_reason") is not None:
                if self.terminal_seen:
                    raise StreamProtocolError(
                        "Invalid assistant content event order"
                    )
                self.terminal_seen = True

    def finish(self) -> None:
        """Require a terminal chunk followed by the DONE sentinel."""
        if not self.terminal_seen or not self.done_seen:
            raise StreamProtocolError("Invalid assistant content event order")


_anthropic_validator: ContextVar[Optional[AnthropicSSEValidator]] = ContextVar(
    "anthropic_sse_validator",
    default=None,
)
_openai_validator: ContextVar[Optional[OpenAIStreamValidator]] = ContextVar(
    "openai_stream_validator",
    default=None,
)


def begin_anthropic_stream() -> None:
    _anthropic_validator.set(AnthropicSSEValidator())


def end_anthropic_stream() -> None:
    _anthropic_validator.set(None)


def begin_openai_stream() -> None:
    _openai_validator.set(OpenAIStreamValidator())


def end_openai_stream() -> None:
    _openai_validator.set(None)


def validate_live_anthropic_event(
    event_type: str,
    data: dict[str, Any],
) -> None:
    """Validate emitted Anthropic events within the current request context."""
    validator = _anthropic_validator.get()
    if validator is None:
        return
    if event_type == "message_start":
        if validator.message_started and not validator.message_stopped:
            raise StreamProtocolError("Invalid assistant content event order")
    validator.accept(event_type, data)
    if event_type in {"message_stop", "error"}:
        _anthropic_validator.set(None)


def validate_anthropic_records(records: list[dict[str, Any]]) -> None:
    """Validate sanitized replay records containing Anthropic SSE chunks."""
    validator = AnthropicSSEValidator()
    for record in records:
        payload = base64.b64decode(record.get("payload_base64", ""))
        for event_type, data in _parse_anthropic_chunk(payload):
            validator.accept(event_type, data)
    validator.finish()


def validate_live_openai_payload(
    payload: Optional[dict[str, Any]],
    done: bool = False,
) -> None:
    """Validate emitted OpenAI chunks within the current request context."""
    validator = _openai_validator.get()
    if validator is None:
        validator = OpenAIStreamValidator()
        _openai_validator.set(validator)
    validator.accept(payload, done=done)
    if done:
        validator.finish()
        _openai_validator.set(None)


def _parse_anthropic_chunk(chunk: bytes) -> list[tuple[str, dict[str, Any]]]:
    text = chunk.decode("utf-8", errors="strict")
    event_type: Optional[str] = None
    parsed: list[tuple[str, dict[str, Any]]] = []
    for line in text.splitlines():
        if line.startswith("event:"):
            event_type = line.removeprefix("event:").strip()
        elif line.startswith("data:") and event_type is not None:
            payload = json.loads(line.removeprefix("data:").strip())
            parsed.append((event_type, payload))
            event_type = None
    return parsed
