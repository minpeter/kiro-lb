# -*- coding: utf-8 -*-

"""
The <thinking> content-fold is removed entirely.

Signed reasoning rides the official nested field. Unsigned reasoning (OpenAI,
or any thinking block without a signature) is now dropped, never folded into
content as a <thinking> tag. The flag KIRO_FOLD_UNSIGNED_REASONING and the
fold_reasoning_into_content helper are gone; nothing in the gateway should
produce a <thinking> tag for a prior turn.
"""

from kiro import config as _config
from kiro.converters_core import build_kiro_history
from kiro.converters_openai import convert_openai_messages_to_unified
from kiro.models_openai import ChatMessage

REASONING = "The user wants probe.txt. I will call read with path=probe.txt."


def _history_entry():
    _, unified = convert_openai_messages_to_unified(
        [
            ChatMessage(role="user", content="Read probe.txt"),
            ChatMessage(role="assistant", content="Reading it now.", reasoning=REASONING),
            ChatMessage(role="user", content="thanks"),
        ]
    )
    history = build_kiro_history(unified, "claude-opus-5")
    return next(e["assistantResponseMessage"] for e in history if "assistantResponseMessage" in e)


def test_no_thinking_tag_is_ever_produced():
    """Unsigned reasoning is dropped; no <thinking> tag appears in content."""
    entry = _history_entry()

    assert "reasoningContent" not in entry
    assert "<thinking>" not in entry["content"]
    assert REASONING not in entry["content"]
    assert entry["content"] == "Reading it now."


def test_fold_helper_is_removed():
    """fold_reasoning_into_content must no longer exist."""
    import kiro.converters_core as cc

    assert not hasattr(cc, "fold_reasoning_into_content")


def test_fold_flag_is_removed():
    """KIRO_FOLD_UNSIGNED_REASONING must no longer exist in config."""
    assert not hasattr(_config, "KIRO_FOLD_UNSIGNED_REASONING")
