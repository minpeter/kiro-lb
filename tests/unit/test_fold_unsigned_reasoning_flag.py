# -*- coding: utf-8 -*-

"""
Default-off for folding unsigned reasoning into content.

OpenAI carries no thinking signature, so its prior-turn reasoning can never use
the nested field and would otherwise be folded into content as <thinking> tags.
That trades context space on every turn for reasoning that is only a summary,
and the only verified benefit of folding is that the tag does not leak. Default
is therefore to drop unsigned reasoning; KIRO_FOLD_UNSIGNED_REASONING=true opts
back into the fold for callers that measured a win from it.

Signed reasoning (Anthropic) is unaffected: it rides the nested field.
"""

import pytest

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


@pytest.fixture(autouse=True)
def _reset_flag(monkeypatch):
    monkeypatch.setattr(_config, "KIRO_FOLD_UNSIGNED_REASONING", False, raising=False)
    yield


def test_unsigned_reasoning_is_dropped_by_default():
    """Default: unsigned reasoning never becomes a <thinking> tag in content."""
    entry = _history_entry()

    assert "reasoningContent" not in entry
    assert "<thinking>" not in entry["content"]
    assert REASONING not in entry["content"]
    assert entry["content"] == "Reading it now."


def test_fold_opt_in_restores_folding(monkeypatch):
    """Opt-in: KIRO_FOLD_UNSIGNED_REASONING=true folds unsigned reasoning as before."""
    monkeypatch.setattr(_config, "KIRO_FOLD_UNSIGNED_REASONING", True, raising=False)

    entry = _history_entry()

    assert "reasoningContent" not in entry
    assert "<thinking>" in entry["content"]
    assert REASONING in entry["content"]


def test_flag_defaults_to_false():
    """The config default must be false, not merely the fixture's value."""
    import importlib

    importlib.reload(_config)
    assert _config.KIRO_FOLD_UNSIGNED_REASONING is False
