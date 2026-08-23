# -*- coding: utf-8 -*-
"""
Payload size guard for Kiro API requests.

The Kiro API rejects oversized payloads with 400
"Input content length exceeds threshold." (reason:
CONTENT_LENGTH_EXCEEDS_THRESHOLD). That name is not a wire-byte count: on
runtime.us-east-1.kiro.dev / generateAssistantResponse / claude-haiku-4.5 the
reject boundary tracks cl100k tokens of the compact JSON (~195_000 pass,
~200_000 fail). This module provides:
- Pre-flight token (and legacy byte) checking
- Auto-trimming of oldest history entries to fit under the limit
"""

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class PayloadTrimStats:
    """Statistics from a payload trim operation."""

    original_bytes: int
    final_bytes: int
    original_entries: int
    final_entries: int
    trimmed: bool
    original_tokens: int = 0
    final_tokens: int = 0


class PayloadTooLargeError(Exception):
    """Raised when a payload exceeds the limit and auto-trimming is disabled.

    Kiro answers an oversized payload with CONTENT_LENGTH_EXCEEDS_THRESHOLD,
    which names neither the size nor the limit. Failing here instead keeps the
    actual numbers in the message so the caller can act on them.
    """

    payload_bytes: int
    limit_bytes: int
    payload_tokens: int
    limit_tokens: int
    unit: str

    def __init__(
        self,
        payload_size: int,
        limit: int,
        *,
        unit: str = "bytes",
        payload_bytes: Optional[int] = None,
        payload_tokens: Optional[int] = None,
    ) -> None:
        self.unit = unit
        if unit == "tokens":
            self.payload_tokens = payload_size
            self.limit_tokens = limit
            self.payload_bytes = payload_bytes or 0
            self.limit_bytes = 0
            quantity = "tokens"
            unit_word = "token"
        else:
            self.payload_bytes = payload_size
            self.limit_bytes = limit
            self.payload_tokens = payload_tokens or 0
            self.limit_tokens = 0
            quantity = "bytes"
            unit_word = "byte"
        super().__init__(
            f"Request payload is {payload_size} {quantity}, over the {limit} {unit_word} limit Kiro accepts. "
            f"Shorten the conversation or send fewer tools. Set AUTO_TRIM_PAYLOAD=true to drop the "
            f"oldest history instead (this silently loses earlier context)."
        )


def _payload_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def check_payload_size(payload: Dict[str, Any]) -> int:
    """Return the serialized UTF-8 byte size of the compact JSON payload.

    ensure_ascii=False matches the decoded Unicode the upstream tokenizer sees
    after JSON parse. The default True would count a Hangul syllable as the 6
    bytes of a \\uXXXX escape instead of one cl100k token.
    """
    return len(_payload_json(payload).encode("utf-8"))


def check_payload_tokens(payload: Dict[str, Any]) -> int:
    """Return cl100k tokens of the compact JSON, without the CJK slope correction.

    Measured 2026-08-23 against runtime.us-east-1.kiro.dev generateAssistantResponse
    (claude-haiku-4.5, no tools): a Hangul JSON of 195_000 chars returned 200, and
    200_000 chars returned 400 CONTENT_LENGTH_EXCEEDS_THRESHOLD. Repeated ASCII
    ``x`` passed at 1_550_000 chars (~193_750 cl100k tokens) and failed at
    1_575_000 (~196_875). Cycling ``abcdefghijklmnopqrstuvwxyz`` of 1_550_000
    chars failed, so the limit is tokenizer units, not wire bytes or Unicode
    scalars. The Claude CJK slope (1.15) is a local estimator for usage display
    and must not be applied here: it would reject the Hangul payload that passed.
    """
    from kiro.tokenizer import count_tokens

    return count_tokens(_payload_json(payload), apply_claude_correction=False, model="claude-haiku-4.5")


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


def _over_limit(payload: Dict[str, Any], max_bytes: Optional[int], max_tokens: Optional[int]) -> bool:
    if max_tokens is not None and check_payload_tokens(payload) > max_tokens:
        return True
    if max_bytes is not None and check_payload_size(payload) > max_bytes:
        return True
    return False


def trim_payload_to_limit(
    payload: Dict[str, Any],
    max_bytes: Optional[int] = None,
    max_tokens: Optional[int] = None,
) -> PayloadTrimStats:
    """
    Trim oldest history entries so the payload fits under max_tokens and/or max_bytes.

    Trims in user/assistant pairs (2 entries at a time), aligns start to
    userInputMessage, and repairs orphaned toolResults after trimming.
    """
    original_bytes = check_payload_size(payload)
    original_tokens = check_payload_tokens(payload)
    history = payload.get("conversationState", {}).get("history")

    if not history:
        return PayloadTrimStats(
            original_bytes=original_bytes,
            final_bytes=original_bytes,
            original_entries=0,
            final_entries=0,
            trimmed=False,
            original_tokens=original_tokens,
            final_tokens=original_tokens,
        )

    original_entries = len(history)

    # Strip empty toolUses before measuring
    _strip_empty_tool_uses(history)

    # Trim pairs from the beginning until under limit (keep at least 2 entries)
    while len(history) > 2 and _over_limit(payload, max_bytes, max_tokens):
        # Remove 2 entries (a user/assistant pair)
        history.pop(0)
        history.pop(0)

    # Align to userInputMessage boundary
    _align_to_user_message(history)

    # Repair orphaned tool results after trimming
    _repair_orphaned_tool_results(history)

    final_bytes = check_payload_size(payload)
    final_tokens = check_payload_tokens(payload)
    return PayloadTrimStats(
        original_bytes=original_bytes,
        final_bytes=final_bytes,
        original_entries=original_entries,
        final_entries=len(history),
        trimmed=original_entries != len(history),
        original_tokens=original_tokens,
        final_tokens=final_tokens,
    )
