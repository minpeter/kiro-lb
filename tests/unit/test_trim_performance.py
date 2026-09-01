# -*- coding: utf-8 -*-
"""The trim must respect the cap without re-tokenizing the payload per iteration."""

import time

from kiro import payload_guards as pg

CHUNK = "public void onDamage(EntityDamageEvent event) { if (event.getCause() == VOID) { cancel(); } } "


def _payload(pairs: int):
    history = []
    for i in range(pairs):
        history.append({"userInputMessage": {"content": CHUNK * 12, "modelId": "claude-opus-5"}})
        history.append({"assistantResponseMessage": {"content": CHUNK * 12}})
    return {
        "conversationState": {
            "conversationId": "trim-test",
            "currentMessage": {"userInputMessage": {"content": "e agora?", "modelId": "claude-opus-5"}},
            "history": history,
        }
    }


class TestTrimCorrectness:
    def test_result_is_under_the_token_cap(self):
        payload = _payload(200)
        original_tokens = pg.check_payload_tokens(payload)
        cap = original_tokens // 3

        stats = pg.trim_payload_to_limit(payload, max_bytes=None, max_tokens=cap)

        assert stats.trimmed
        assert stats.final_tokens <= cap, f"{stats.final_tokens} > {cap}"
        assert pg.check_payload_tokens(payload) <= cap

    def test_result_is_under_the_byte_cap(self):
        payload = _payload(200)
        cap = pg.check_payload_size(payload) // 3

        stats = pg.trim_payload_to_limit(payload, max_bytes=cap, max_tokens=None)

        assert stats.final_bytes <= cap
        assert pg.check_payload_size(payload) <= cap

    def test_history_starts_with_a_user_message(self):
        payload = _payload(200)
        cap = pg.check_payload_tokens(payload) // 4
        pg.trim_payload_to_limit(payload, max_bytes=None, max_tokens=cap)
        history = payload["conversationState"].get("history") or []
        if history:
            assert "userInputMessage" in history[0]

    def test_payload_already_under_the_cap_is_untouched(self):
        payload = _payload(4)
        before = len(payload["conversationState"]["history"])
        stats = pg.trim_payload_to_limit(payload, max_bytes=None, max_tokens=10_000_000)
        assert not stats.trimmed
        assert len(payload["conversationState"]["history"]) == before


class TestTrimCost:
    def test_full_payload_is_not_retokenized_per_iteration(self, monkeypatch):
        """Replacing 100+ full passes with a handful was the point of the change."""
        payload = _payload(200)
        cap = pg.check_payload_tokens(payload) // 3

        calls = {"n": 0}
        real = pg.check_payload_tokens

        def counting(p):
            calls["n"] += 1
            return real(p)

        monkeypatch.setattr(pg, "check_payload_tokens", counting)
        stats = pg.trim_payload_to_limit(payload, max_bytes=None, max_tokens=cap)

        assert stats.trimmed
        # Before: one call per removed pair (dozens). Now: the two reporting
        # measurements plus, at most, a few exact verifications.
        assert calls["n"] <= 6, f"{calls['n']} full tokenization passes"

    def test_trim_of_a_large_history_is_quick(self):
        payload = _payload(400)
        cap = pg.check_payload_tokens(payload) // 4

        started = time.perf_counter()
        pg.trim_payload_to_limit(payload, max_bytes=None, max_tokens=cap)
        elapsed = time.perf_counter() - started

        assert elapsed < 10.0, f"trim levou {elapsed:.1f}s"
