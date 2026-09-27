# -*- coding: utf-8 -*-
"""
Payload size guard for Kiro API requests.

The Kiro API rejects oversized payloads with 400
"Input content length exceeds threshold." (reason:
CONTENT_LENGTH_EXCEEDS_THRESHOLD). That name is not a wire-byte count: on
runtime.us-east-1.kiro.dev / generateAssistantResponse the reject boundary
tracks cl100k tokens of the compact JSON. claude-opus-5: 800_000 Hangul
pass, 1_000_000 fail. This module provides:
- Pre-flight token (and legacy byte) checking
- Auto-trimming of oldest history entries to fit under the limit

Image base64 is not part of that count. Measured 2026-09-27 on
runtime.us-east-1.kiro.dev / claude-haiku-4.5 (text threshold ~195k tokens): a
1.2 MB and a 2.9 MB PNG (1.17M and 2.82M cl100k tokens of base64) both returned
200, and contextUsage grew by ~690 and ~1,530 tokens over a text-only control,
the Anthropic ceil(w*h/750) rate. The only image limit hit was
IMAGE_SIZE_EXCEEDED at 5 MiB of base64 per image, a separate upstream check that
names both numbers. Images are therefore measured by that vision estimate and
their data is left out of both the token and the byte count.
"""

import base64
import binascii
import json
import math
import struct
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# Upper end of Anthropic's per-image cost after its ~1.15 MP resize; used when
# the dimensions cannot be read from the image header.
IMAGE_TOKENS_UNKNOWN_SIZE = 1600

# Decoded header window for dimension sniffing. JPEG SOF can sit behind a large
# EXIF segment; anything beyond this falls back to the constant.
_HEADER_B64_CHARS = 87_384

_JPEG_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


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


def _image_dimensions(head: bytes) -> Optional[Tuple[int, int]]:
    """Read (width, height) from a PNG, JPEG, GIF or WebP header."""
    if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR" and len(head) >= 24:
        width, height = struct.unpack(">II", head[16:24])
        return width, height
    if head[:6] in (b"GIF87a", b"GIF89a") and len(head) >= 10:
        width, height = struct.unpack("<HH", head[6:10])
        return width, height
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP" and len(head) >= 30:
        chunk = head[12:16]
        if chunk == b"VP8X":
            return 1 + int.from_bytes(head[24:27], "little"), 1 + int.from_bytes(head[27:30], "little")
        if chunk == b"VP8 " and head[23:26] == b"\x9d\x01\x2a":
            width, height = struct.unpack("<HH", head[26:30])
            return width & 0x3FFF, height & 0x3FFF
        if chunk == b"VP8L" and head[20] == 0x2F:
            bits = int.from_bytes(head[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        return None
    if head[:2] == b"\xff\xd8":
        index = 2
        while index + 9 <= len(head):
            if head[index] != 0xFF:
                return None
            marker = head[index + 1]
            if marker == 0xFF:
                index += 1
                continue
            if marker in _JPEG_SOF_MARKERS:
                height, width = struct.unpack(">HH", head[index + 5 : index + 9])
                return width, height
            if marker == 0x01 or 0xD0 <= marker <= 0xD8:
                index += 2
                continue
            (length,) = struct.unpack(">H", head[index + 2 : index + 4])
            index += 2 + length
    return None


def estimate_image_tokens(data: str) -> int:
    """Vision tokens for one base64 image: ceil(w*h/750), else the constant.

    Not clamped to Anthropic's resize: models that accept higher resolutions
    charge more, and over-counting is the safe direction for a pre-flight cap.
    """
    prefix = data[:_HEADER_B64_CHARS]
    try:
        head = base64.b64decode(prefix[: len(prefix) - len(prefix) % 4])
    except (binascii.Error, ValueError):
        return IMAGE_TOKENS_UNKNOWN_SIZE
    dimensions = _image_dimensions(head)
    if not dimensions or not all(dimensions):
        return IMAGE_TOKENS_UNKNOWN_SIZE
    width, height = dimensions
    return math.ceil(width * height / 750)


def _entry_images(entry: Any) -> List[Dict[str, Any]]:
    """Image sources of one history/current entry (userInputMessage.images)."""
    user = entry.get("userInputMessage") if isinstance(entry, dict) else None
    if not isinstance(user, dict):
        return []
    sources = []
    for image in user.get("images") or []:
        source = image.get("source") if isinstance(image, dict) else None
        if isinstance(source, dict) and isinstance(source.get("bytes"), str):
            sources.append(source)
    return sources


def _payload_images(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    state = payload.get("conversationState")
    if not isinstance(state, dict):
        return []
    entries = list(state.get("history") or [])
    entries.append(state.get("currentMessage"))
    return [source for entry in entries for source in _entry_images(entry)]


def _measured_json(obj: Dict[str, Any], sources: List[Dict[str, Any]]) -> Tuple[str, int]:
    """Serialize with image data blanked, returning (json, image tokens).

    The data is swapped out and restored in place rather than deep-copying a
    payload that can be megabytes of text.
    """
    saved = [source["bytes"] for source in sources]
    for source in sources:
        source["bytes"] = ""
    try:
        serialized = _payload_json(obj)
    finally:
        for source, data in zip(sources, saved):
            source["bytes"] = data
    return serialized, sum(estimate_image_tokens(data) for data in saved)


def measure_payload(payload: Dict[str, Any]) -> tuple[int, int]:
    """Return (tokens, bytes) from a single serialization of the payload.

    check_payload_tokens() and check_payload_size() each serialized the payload
    independently, so the pre-flight guard paid the dump twice per request.
    """
    serialized, image_tokens = _measured_json(payload, _payload_images(payload))
    from kiro.tokenizer import count_tokens

    tokens = count_tokens(serialized, apply_claude_correction=False, model="claude-haiku-4.5")
    return tokens + image_tokens, len(serialized.encode("utf-8"))


def check_payload_size(payload: Dict[str, Any]) -> int:
    """Return the serialized UTF-8 byte size of the compact JSON payload.

    ensure_ascii=False matches the decoded Unicode the upstream tokenizer sees
    after JSON parse. The default True would count a Hangul syllable as the 6
    bytes of a \\uXXXX escape instead of one cl100k token. Image data is left
    out: upstream bounds it per image (IMAGE_SIZE_EXCEEDED), not in this total.
    """
    serialized, _ = _measured_json(payload, _payload_images(payload))
    return len(serialized.encode("utf-8"))


def payload_token_limit_for_model(model_id: str) -> int:
    """Return the pre-flight token cap.

    Single default: 800_000, the largest claude-opus-5 Hangul JSON measured to
    pass (1_000_000 returned CONTENT_LENGTH_EXCEEDS_THRESHOLD). Override with
    KIRO_MAX_PAYLOAD_TOKENS.
    """
    from kiro.config import KIRO_MAX_PAYLOAD_TOKENS

    return KIRO_MAX_PAYLOAD_TOKENS


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
    Images count by estimate_image_tokens(), not by their base64.
    """
    return measure_payload(payload)[0]


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


def _repair_orphaned_tool_results(history: list, current_message: Optional[Dict[str, Any]] = None) -> None:
    """
    Remove orphaned toolResults that reference toolUseIds not present
    in the preceding assistant message. Preserve orphaned text content
    inline with a marker.
    """
    entries = history + ([current_message] if current_message else [])
    for i, entry in enumerate(entries):
        user_msg = entry.get("userInputMessage")
        if not user_msg:
            continue

        ctx = user_msg.get("userInputMessageContext")
        if not ctx or "toolResults" not in ctx:
            continue

        # Collect toolUseIds from the preceding assistant message
        valid_ids = set()
        if i > 0:
            prev_assistant = entries[i - 1].get("assistantResponseMessage")
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


def _drop_pairs_by_estimate(
    payload: Dict[str, Any],
    history: list,
    max_bytes: Optional[int],
    max_tokens: Optional[int],
    total_tokens: int,
    total_bytes: int,
) -> None:
    """Drop old pairs using estimated cost, without any extra full pass.

    The original loop called _over_limit() per iteration, and each call
    serialized and tokenized the whole payload: 113 iterations on a 3 MB payload
    cost ~44s before the request even left.

    Each entry's byte cost comes from its own cheap serialization, and its token
    share is prorated from the payload's real token total. Tokenizing every entry
    would be more precise but costs another full pass; the proration is close
    enough and the caller's exact check corrects the rest.

    The 3% margin keeps a proration error from leaving the payload just above the
    cap, which would force exact iterations - the expensive ones.
    """
    if not history:
        return

    entry_bytes = [len(_measured_json(entry, _entry_images(entry))[0].encode("utf-8")) + 1 for entry in history]
    history_bytes = sum(entry_bytes)
    if history_bytes <= 0:
        return

    tokens_per_byte = total_tokens / total_bytes if total_bytes else 0.0
    remaining_tokens = float(total_tokens)
    remaining_bytes = total_bytes

    token_target = max_tokens * 0.97 if max_tokens is not None else None

    index = 0
    while index < len(history):
        over_tokens = token_target is not None and remaining_tokens > token_target
        over_bytes = max_bytes is not None and remaining_bytes > max_bytes
        if not (over_tokens or over_bytes):
            break
        for _ in range(2):
            if index < len(history):
                remaining_tokens -= entry_bytes[index] * tokens_per_byte
                remaining_bytes -= entry_bytes[index]
                index += 1

    if index:
        del history[:index]


def trim_payload_to_limit(
    payload: Dict[str, Any],
    max_bytes: Optional[int] = None,
    max_tokens: Optional[int] = None,
    known_tokens: Optional[int] = None,
    known_bytes: Optional[int] = None,
) -> PayloadTrimStats:
    """
    Trim oldest history entries so the payload fits under max_tokens and/or max_bytes.

    Trims in user/assistant pairs (2 entries at a time), aligns start to
    userInputMessage, and repairs orphaned toolResults after trimming.

    ``known_tokens``/``known_bytes`` reuse the measurement the caller already did
    in the pre-flight guard. Without them, measuring again costs a full
    serialization and tokenization pass over the whole payload.
    """
    if known_tokens is not None and known_bytes is not None:
        original_tokens, original_bytes = known_tokens, known_bytes
    else:
        original_tokens, original_bytes = measure_payload(payload)
    conversation_state = payload.get("conversationState", {})
    history = conversation_state.get("history")

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

    # Trim pairs from the beginning until under limit or no history remains.
    # The per-entry estimate handles the bulk without re-tokenizing the whole
    # payload; the exact check below covers the tokenizer's boundary difference
    # and normally needs no extra iteration.
    _drop_pairs_by_estimate(payload, history, max_bytes, max_tokens, original_tokens, original_bytes)
    while history and _over_limit(payload, max_bytes, max_tokens):
        del history[:2]

    # Align to userInputMessage boundary
    _align_to_user_message(history)

    # Repair orphaned tool results after trimming
    _repair_orphaned_tool_results(history, conversation_state.get("currentMessage"))

    if not history:
        del conversation_state["history"]

    final_tokens, final_bytes = measure_payload(payload)
    return PayloadTrimStats(
        original_bytes=original_bytes,
        final_bytes=final_bytes,
        original_entries=original_entries,
        final_entries=len(history),
        trimmed=original_entries != len(history),
        original_tokens=original_tokens,
        final_tokens=final_tokens,
    )
