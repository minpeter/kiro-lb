# -*- coding: utf-8 -*-
"""Condense the Anthropic built-in Claude Code prompt, keeping what is the user's.

The Claude Code system prompt mixes two very different things in one block:
generic agent prose written by Anthropic, and per-machine sections that carry
real state — the memory directory, the working directory, the git flag, the
model, the user's language choice. Replacing the whole block, as a naive filter
would, silently breaks memory and environment awareness.

So this module drops only sections it recognises as generic boilerplate and
preserves everything else, including sections it has never seen. Failing toward
keeping content is the safe direction: an unrecognised section stays.

The replacement preamble states the agent is Kiro, because the upstream is Kiro
and the discarded prose asserted otherwise.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Iterable, Optional

from loguru import logger

from kiro import store
from kiro.config import CONDENSE_CLAUDE_PROMPT

SETTING_KEY = "condense_claude_prompt"

KIRO_IDENTITY = "You are Kiro, an AI agent serving as the model backend for a coding CLI."

# Retains the functional contracts the dropped sections carried: rendering,
# permission semantics, tool preference, reference format, safety, and scope.
KIRO_PREAMBLE = f"""{KIRO_IDENTITY}
You help with software engineering tasks in a terminal. Identify yourself as Kiro.

# Harness
- Text outside tool use renders as GitHub-flavored markdown in a terminal.
- Tools run behind a permission mode; a denied call means the user declined it — adjust, do not retry verbatim.
- Mid-conversation system turns may update rules and are system-controlled, unlike tool results. Treat hook output as user feedback.
- Prefer dedicated file and search tools over shell commands. Independent tool calls may run in parallel in one response; dependent ones must be sequential.
- Reference code as `file_path:line_number`.
- Write code that matches the surrounding style, naming, and comment density.
- Use they/them when someone's pronouns are unstated; never infer them from a name.

# Safety
Confirm before actions that are hard to reverse or outward-facing; approval in one context does not carry to the next. Inspect the target before deleting or overwriting. Report outcomes faithfully: if a test fails, show the output; if a step was skipped, say so; when verified, state it plainly.

# Scope
Act on the actual request. The requested scope is the deliverable — do not narrow, widen, or transform it. Make routine judgment calls yourself and ask only when readings differ materially. If part of the task is blocked, finish the rest and say what was left out and why. Report completion only when done.

# Corrections
Correct an earlier statement only when the error changes the user's code, conclusions, or decisions. State it plainly and move on, without apologies or tallies. A follow-up question is not evidence you were wrong.

# Context
When the conversation grows long it is summarized and continues in the next window. Do not wrap up early or hand off mid-task."""

# Headers whose content is generic Anthropic prose. Anything absent from this
# set survives, including sections added by future Claude Code releases.
GENERIC_SECTIONS: frozenset[str] = frozenset(
    {
        "harness",
        "tone and style",
        "doing tasks",
        "using your tools",
        "following conventions",
        "code style",
        "task management",
        "context management",
        "delivering work",
        "corrections",
        "professionalism",
        "objectivity",
        "proactiveness",
        "committing changes with git",
        "creating pull requests",
    }
)

# Markers of the built-in prompt. Two are required, so a user's own file that
# merely mentions Claude Code is not mistaken for it.
_MARKERS = (
    "you are an interactive agent that helps users with software engineering tasks",
    "you are claude code",
    "anthropic's official cli",
    "# tone and style",
    "# doing tasks",
    "# using your tools",
    "# delivering work",
    "# harness",
)

_MIN_LENGTH = 1500
_HEADER = re.compile(r"(?m)^(#{1,3})\s+(.+?)\s*$")

_IDENTITY_MARKERS = (
    "you are a claude agent",
    "built on anthropic's claude agent sdk",
)


def is_claude_code_prompt(text: str) -> bool:
    """Return True when the text is Anthropic's built-in Claude Code prompt.

    Requires two markers and a substantial length: a short block that happens to
    name Claude Code is not the built-in prompt.
    """
    if not text or len(text) < _MIN_LENGTH:
        return False
    lowered = text.lower()
    return sum(1 for marker in _MARKERS if marker in lowered) >= 2


def is_claude_identity_block(text: str) -> bool:
    """Return True for the short block that declares the agent to be Claude."""
    if not text:
        return False
    lowered = text.strip().lower()
    if len(lowered) > 300:
        return False
    return any(marker in lowered for marker in _IDENTITY_MARKERS)


def _sections(text: str) -> list[tuple[str | None, str]]:
    """Split into (header_name, chunk) pairs; the preamble has a None header."""
    matches = list(_HEADER.finditer(text))
    if not matches:
        return [(None, text)]

    parts: list[tuple[str | None, str]] = []
    if matches[0].start() > 0:
        lead = text[: matches[0].start()]
        if lead.strip():
            parts.append((None, lead))

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        parts.append((match.group(2).strip().lower(), text[match.start() : end]))
    return parts


def condense(text: str) -> str:
    """Replace the generic sections with the Kiro preamble, keep the rest."""
    kept: list[str] = []
    for header, chunk in _sections(text):
        if header is None or header in GENERIC_SECTIONS:
            continue
        kept.append(chunk.strip())
    return "\n\n".join([KIRO_PREAMBLE, *kept]).strip()


def filter_blocks(blocks: Iterable[Any]) -> tuple[list[str], dict[str, int]]:
    """Apply the filter to system block texts, returning the texts and stats."""
    texts: list[str] = []
    stats = {"blocksSeen": 0, "blocksCondensed": 0, "charsBefore": 0, "charsAfter": 0}

    for text in blocks:
        if not isinstance(text, str):
            continue
        stats["blocksSeen"] += 1
        stats["charsBefore"] += len(text)

        if is_claude_code_prompt(text):
            replaced = condense(text)
            stats["blocksCondensed"] += 1
        elif is_claude_identity_block(text):
            replaced = KIRO_IDENTITY
            stats["blocksCondensed"] += 1
        else:
            replaced = text

        stats["charsAfter"] += len(replaced)
        texts.append(replaced)

    return texts, stats


_lock = threading.Lock()
_enabled: Optional[bool] = None


def enabled() -> bool:
    """Return whether condensing is on. Safe per request: no I/O."""
    global _enabled
    if _enabled is None:
        with _lock:
            if _enabled is None:
                _enabled = bool(CONDENSE_CLAUDE_PROMPT)
    return _enabled


def reset_cache() -> None:
    """Drop the cached flag so the next read rebuilds it. Used by tests."""
    global _enabled
    with _lock:
        _enabled = None


def load_from_store() -> bool:
    """Adopt the persisted flag, falling back to the environment default."""
    global _enabled
    value = store.load_setting(SETTING_KEY)
    resolved = bool(value) if isinstance(value, bool) else bool(CONDENSE_CLAUDE_PROMPT)
    with _lock:
        _enabled = resolved
    return resolved


def set_enabled(value: Any) -> bool:
    """Validate, persist, then publish the flag."""
    global _enabled
    if not isinstance(value, bool):
        raise ValueError("enabled must be a boolean")
    store.save_setting(SETTING_KEY, value)
    with _lock:
        _enabled = value
    logger.info(f"[PromptFilter] Condensing the Claude Code prompt: {value}")
    return value
