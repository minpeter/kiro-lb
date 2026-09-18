# -*- coding: utf-8 -*-
"""Translate a Chat Completions SSE stream into Responses API events.

The gateway has one generation pipeline, and it speaks Chat Completions chunks.
This module consumes those chunks and re-emits the same turn as the item-centered
event sequence the Responses API defines:

    response.created
    response.output_item.added        (reasoning, when the model reasons)
    response.reasoning_summary_text.delta ...
    response.reasoning_summary_text.done
    response.output_item.done
    response.output_item.added        (assistant message)
    response.content_part.added
    response.output_text.delta ...
    response.output_text.done
    response.content_part.done
    response.output_item.done
    response.output_item.added/.done  (one pair per function call)
    response.completed

Why translate the serialized chunks instead of tapping the generator directly:
`stream_kiro_to_openai_internal` validates its own output against the
chat-completion chunk grammar (`kiro/sse_validation.py`), keyed to the
begin/end calls inside that generator. Pushing Responses payloads through the
same emitter would trip that validator on every event. Translating downstream
costs one json round trip per chunk and leaves the validation intact.

Events the Codex CLI ignores (`content_part.*`, `output_text.done`,
`function_call_arguments.*`) are emitted anyway: the official OpenAI SDK consumes
them, and the cost is one line each.

The chat stream's `[DONE]` sentinel is consumed, not forwarded. A Responses stream
ends at `response.completed`.
"""

from __future__ import annotations

import json
from typing import Any, AsyncGenerator, AsyncIterable, Dict, FrozenSet, List, Optional

from loguru import logger

from kiro.converters_responses import (
    message_item,
    new_item_id,
    new_response_id,
    output_item_for,
    reasoning_item,
    response_envelope,
    usage_block,
)

# Status codes whose failures a client should retry rather than surface. Codex reads
# this code and backs off instead of ending the turn.
_RETRYABLE_ERROR_CODES = {429: "rate_limit_exceeded", 503: "server_error", 500: "server_error"}


def _sse(event_type: str, payload: Dict[str, Any]) -> str:
    """Serialize one Responses event.

    The `type` and `sequence_number` are part of the payload, and the SSE `event:`
    line repeats the type: the OpenAI SDK dispatches on the field, some HTTP
    clients on the line, and emitting both costs nothing.
    """
    body = {"type": event_type, **payload}
    return f"event: {event_type}\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"


class _Sequencer:
    """Monotonic event counter.

    The Responses API numbers every event of a stream, and clients use the numbers
    to detect gaps, so the counter has to be shared by all emitters.
    """

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = -1

    def next(self) -> int:
        self.value += 1
        return self.value


def _parse_chunk(line: str) -> Optional[Dict[str, Any]]:
    """Extract the JSON payload from one `data:` line, or None for anything else."""
    if not line.startswith("data:"):
        return None
    body = line[len("data:") :].strip()
    if not body or body == "[DONE]":
        return None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        logger.debug("Skipping unparsable chat chunk while translating to Responses")
        return None
    return parsed if isinstance(parsed, dict) else None


def _iter_payloads(buffer: str) -> tuple[List[Dict[str, Any]], str]:
    """Split a buffer into complete SSE payloads plus the unterminated remainder.

    Chunks arrive on transport boundaries, not event boundaries, so a payload can
    be split across two reads. Everything up to the last blank-line separator is
    complete; the tail is carried into the next read.
    """
    payloads: List[Dict[str, Any]] = []
    while "\n\n" in buffer:
        block, buffer = buffer.split("\n\n", 1)
        for line in block.splitlines():
            parsed = _parse_chunk(line.strip())
            if parsed is not None:
                payloads.append(parsed)
    return payloads, buffer


async def translate_chat_stream_to_responses(
    chunks: AsyncIterable[Any],
    model: str,
    response_id: Optional[str] = None,
    freeform_tools: FrozenSet[str] = frozenset(),
) -> AsyncGenerator[str, None]:
    """Re-emit a Chat Completions SSE stream as Responses API events.

    ``freeform_tools`` names the tools the request declared as ``custom``. A call to
    one of those is reported as a ``custom_tool_call`` item rather than a
    ``function_call``, because the client dispatches on the item type.
    """
    response_id = response_id or new_response_id()
    seq = _Sequencer()

    message_id = new_item_id("msg")
    reasoning_id = new_item_id("rs")

    text_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    usage: Optional[Dict[str, Any]] = None
    finish_reason: Optional[str] = None

    reasoning_open = False
    message_open = False
    buffer = ""

    yield _sse(
        "response.created",
        {
            "sequence_number": seq.next(),
            "response": response_envelope(response_id, model, "in_progress", []),
        },
    )
    yield _sse(
        "response.in_progress",
        {
            "sequence_number": seq.next(),
            "response": response_envelope(response_id, model, "in_progress", []),
        },
    )

    def open_message() -> List[str]:
        """Emit the events that must precede the first text delta."""
        events = [
            _sse(
                "response.output_item.added",
                {
                    "sequence_number": seq.next(),
                    "output_index": 0,
                    "item": {
                        "id": message_id,
                        "type": "message",
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                    },
                },
            ),
            _sse(
                "response.content_part.added",
                {
                    "sequence_number": seq.next(),
                    "item_id": message_id,
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                },
            ),
        ]
        return events

    def close_reasoning() -> List[str]:
        text = "".join(reasoning_parts)
        return [
            _sse(
                "response.reasoning_summary_text.done",
                {
                    "sequence_number": seq.next(),
                    "item_id": reasoning_id,
                    "output_index": 0,
                    "summary_index": 0,
                    "text": text,
                },
            ),
            _sse(
                "response.output_item.done",
                {
                    "sequence_number": seq.next(),
                    "output_index": 0,
                    "item": reasoning_item(text, reasoning_id),
                },
            ),
        ]

    try:
        async for raw in chunks:
            buffer += raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
            payloads, buffer = _iter_payloads(buffer)

            for payload in payloads:
                if payload.get("usage"):
                    usage = payload["usage"]

                for choice in payload.get("choices") or []:
                    delta = choice.get("delta") or {}

                    reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                    if reasoning:
                        if not reasoning_open:
                            reasoning_open = True
                            yield _sse(
                                "response.output_item.added",
                                {
                                    "sequence_number": seq.next(),
                                    "output_index": 0,
                                    "item": {
                                        "id": reasoning_id,
                                        "type": "reasoning",
                                        "summary": [],
                                    },
                                },
                            )
                            yield _sse(
                                "response.reasoning_summary_part.added",
                                {
                                    "sequence_number": seq.next(),
                                    "item_id": reasoning_id,
                                    "output_index": 0,
                                    "summary_index": 0,
                                    "part": {"type": "summary_text", "text": ""},
                                },
                            )
                        reasoning_parts.append(str(reasoning))
                        yield _sse(
                            "response.reasoning_summary_text.delta",
                            {
                                "sequence_number": seq.next(),
                                "item_id": reasoning_id,
                                "output_index": 0,
                                "summary_index": 0,
                                "delta": str(reasoning),
                            },
                        )

                    content = delta.get("content")
                    if content:
                        # Reasoning always precedes text upstream, so its item is
                        # closed here rather than at the end: the output list has to
                        # come out in the order the client will replay it.
                        if reasoning_open:
                            reasoning_open = False
                            for event in close_reasoning():
                                yield event
                        if not message_open:
                            message_open = True
                            for event in open_message():
                                yield event
                        text_parts.append(str(content))
                        yield _sse(
                            "response.output_text.delta",
                            {
                                "sequence_number": seq.next(),
                                "item_id": message_id,
                                "output_index": 0,
                                "content_index": 0,
                                "delta": str(content),
                            },
                        )

                    for call in delta.get("tool_calls") or []:
                        if isinstance(call, dict):
                            tool_calls.append(call)

                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
    except Exception as exc:
        # The turn died mid-stream. A Responses client needs response.failed to
        # know that; without it the stream just stops and the client waits.
        logger.error("Responses stream failed while translating: {}", exc)
        code = _RETRYABLE_ERROR_CODES.get(getattr(exc, "status_code", 0), "server_error")
        yield _sse(
            "response.failed",
            {
                "sequence_number": seq.next(),
                "response": response_envelope(
                    response_id,
                    model,
                    "failed",
                    [],
                    error={"code": code, "message": str(exc) or type(exc).__name__},
                ),
            },
        )
        return

    output: List[Dict[str, Any]] = []

    if reasoning_open:
        # Reasoning with no text after it: close the item now.
        reasoning_open = False
        for event in close_reasoning():
            yield event
    if reasoning_parts:
        output.append(reasoning_item("".join(reasoning_parts), reasoning_id))

    text = "".join(text_parts)
    if message_open:
        yield _sse(
            "response.output_text.done",
            {
                "sequence_number": seq.next(),
                "item_id": message_id,
                "output_index": 0,
                "content_index": 0,
                "text": text,
            },
        )
        yield _sse(
            "response.content_part.done",
            {
                "sequence_number": seq.next(),
                "item_id": message_id,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": text, "annotations": []},
            },
        )
        yield _sse(
            "response.output_item.done",
            {
                "sequence_number": seq.next(),
                "output_index": 0,
                "item": message_item(text, message_id),
            },
        )
        output.append(message_item(text, message_id))

    for index, call in enumerate(tool_calls, start=len(output)):
        item = output_item_for(call, new_item_id("fc"), freeform_tools)
        is_freeform = item["type"] == "custom_tool_call"
        # The body field differs with the item type, and so do the argument events:
        # a freeform call streams through custom_tool_call_input.*, which is the pair
        # the client listens on for it.
        body_field = "input" if is_freeform else "arguments"
        delta_event = (
            "response.custom_tool_call_input.delta" if is_freeform else "response.function_call_arguments.delta"
        )
        done_event = "response.custom_tool_call_input.done" if is_freeform else "response.function_call_arguments.done"

        yield _sse(
            "response.output_item.added",
            {
                "sequence_number": seq.next(),
                "output_index": index,
                "item": {**item, "status": "in_progress", body_field: ""},
            },
        )
        yield _sse(
            delta_event,
            {
                "sequence_number": seq.next(),
                "item_id": item["id"],
                "call_id": item["call_id"],
                "output_index": index,
                "delta": item[body_field],
            },
        )
        yield _sse(
            done_event,
            {
                "sequence_number": seq.next(),
                "item_id": item["id"],
                "call_id": item["call_id"],
                "output_index": index,
                body_field: item[body_field],
            },
        )
        yield _sse(
            "response.output_item.done",
            {"sequence_number": seq.next(), "output_index": index, "item": item},
        )
        output.append(item)

    # A truncated turn is incomplete, not completed: reporting it as a clean finish
    # is what makes a client accept half an answer.
    if finish_reason == "length":
        envelope = response_envelope(response_id, model, "incomplete", output, usage=usage_block(usage))
        envelope["incomplete_details"] = {"reason": "max_output_tokens"}
        yield _sse("response.incomplete", {"sequence_number": seq.next(), "response": envelope})
        return

    yield _sse(
        "response.completed",
        {
            "sequence_number": seq.next(),
            "response": response_envelope(response_id, model, "completed", output, usage=usage_block(usage)),
        },
    )
