# -*- coding: utf-8 -*-
"""
Payload size guard for Kiro API requests.

The Kiro API rejects oversized payloads with 400
"Input content length exceeds threshold." (reason:
CONTENT_LENGTH_EXCEEDS_THRESHOLD). Measured boundary: 1,085,435 bytes pass and
1,086,459 bytes fail. This module provides:
- Pre-flight size checking
- Auto-trimming of oldest history entries to fit under the limit

Ported from sametakofficial's payload_guards.py, simplified.
"""

import json
from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class PayloadTrimStats:
    """Statistics from a payload trim operation."""

    original_bytes: int
    final_bytes: int
    original_entries: int
    final_entries: int
    trimmed: bool


class PayloadTooLargeError(Exception):
    """Raised when a payload exceeds the limit and auto-trimming is disabled.

    Kiro answers an oversized payload with CONTENT_LENGTH_EXCEEDS_THRESHOLD,
    which names neither the size nor the limit. Failing here instead keeps the
    actual numbers in the message so the caller can act on them.
    """

    payload_bytes: int
    limit_bytes: int

    def __init__(self, payload_bytes: int, limit_bytes: int) -> None:
        self.payload_bytes = payload_bytes
        self.limit_bytes = limit_bytes
        super().__init__(
            f"Request payload is {payload_bytes} bytes, over the {limit_bytes} byte limit Kiro accepts. "
            f"Shorten the conversation or send fewer tools. Set AUTO_TRIM_PAYLOAD=true to drop the "
            f"oldest history instead (this silently loses earlier context)."
        )


def check_payload_size(payload: Dict[str, Any]) -> int:
    """Return the serialized byte size of the payload as UTF-8 JSON.

    ensure_ascii=False matches how the routes actually serialize the upstream
    body (routes_openai.py, routes_anthropic.py). With the default True, a Hangul
    character measures as the 6 bytes of a \\uXXXX escape instead of the 3 bytes
    UTF-8 puts on the wire, so a Korean conversation was rejected at roughly half
    the size Kiro accepts.
    """
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _strip_empty_tool_uses(history: list) -> None:
    """Remove empty toolUses arrays in-place (Kiro quirk)."""
    for entry in history:
        assistant = entry.get("assistantResponseMessage")
        if assistant and "toolUses" in assistant and assistant["toolUses"] == []:
            del assistant["toolUses"]


def _align_to_user_message(history: list) -> list:
    """Ensure history starts with a userInputMessage entry."""
    while history and "userInputMessage" not in history[0]:
        history.pop(0)
    return history


def _repair_orphaned_tool_results(history: list) -> None:
    """
    Remove orphaned toolResults that reference toolUseIds not present
    in the preceding assistant message. Preserve orphaned text content
    inline with a marker.
    """
    for i, entry in enumerate(history):
        user_msg = entry.get("userInputMessage")
        if not user_msg:
            continue

        ctx = user_msg.get("userInputMessageContext")
        if not ctx or "toolResults" not in ctx:
            continue

        # Collect toolUseIds from the preceding assistant message
        valid_ids = set()
        if i > 0:
            prev_assistant = history[i - 1].get("assistantResponseMessage")
            if prev_assistant:
                for tu in prev_assistant.get("toolUses", []):
                    tool_use_id = tu.get("toolUseId")
                    if tool_use_id:
                        valid_ids.add(tool_use_id)

        kept = []
        orphaned_text_parts = []
        for tr in ctx["toolResults"]:
            if tr.get("toolUseId") in valid_ids:
                kept.append(tr)
            else:
                # Preserve text content from orphaned results
                content = tr.get("content")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("text"):
                            orphaned_text_parts.append(part["text"])
                elif isinstance(content, str) and content:
                    orphaned_text_parts.append(content)

        if len(kept) != len(ctx["toolResults"]):
            if kept:
                ctx["toolResults"] = kept
            else:
                del ctx["toolResults"]
                if not ctx:
                    del user_msg["userInputMessageContext"]

            # Append orphaned text to user message content
            if orphaned_text_parts:
                marker = "\n[trimmed tool result] " + "; ".join(orphaned_text_parts)
                current_content = user_msg.get("content", "")
                user_msg["content"] = current_content + marker


def trim_payload_to_limit(payload: Dict[str, Any], max_bytes: int) -> PayloadTrimStats:
    """
    Trim oldest history entries so the serialized payload fits under max_bytes.

    Trims in user/assistant pairs (2 entries at a time), aligns start to
    userInputMessage, and repairs orphaned toolResults after trimming.
    """
    original_bytes = check_payload_size(payload)
    history = payload.get("conversationState", {}).get("history")

    if not history:
        return PayloadTrimStats(
            original_bytes=original_bytes,
            final_bytes=original_bytes,
            original_entries=0,
            final_entries=0,
            trimmed=False,
        )

    original_entries = len(history)

    # Strip empty toolUses before measuring
    _strip_empty_tool_uses(history)

    # Trim pairs from the beginning until under limit (keep at least 2 entries)
    while len(history) > 2 and check_payload_size(payload) > max_bytes:
        # Remove 2 entries (a user/assistant pair)
        history.pop(0)
        history.pop(0)

    # Align to userInputMessage boundary
    _align_to_user_message(history)

    # Repair orphaned tool results after trimming
    _repair_orphaned_tool_results(history)

    final_bytes = check_payload_size(payload)
    return PayloadTrimStats(
        original_bytes=original_bytes,
        final_bytes=final_bytes,
        original_entries=original_entries,
        final_entries=len(history),
        trimmed=original_entries != len(history),
    )
