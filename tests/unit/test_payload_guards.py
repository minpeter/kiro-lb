# -*- coding: utf-8 -*-

"""
Unit tests for payload size guard logic.
Tests check_payload_size() and trim_payload_to_limit() functions.
"""

import json

import pytest

from kiro.payload_guards import (
    check_payload_size,
    check_payload_tokens,
    payload_token_limit_for_model,
    trim_payload_to_limit,
)


def _make_payload(num_pairs=5, content_size=100):
    """Helper: build a minimal Kiro-shaped payload with N user/assistant pairs."""
    history = []
    for i in range(num_pairs):
        history.append({"userInputMessage": {"content": f"user message {i} " + "x" * content_size}})
        history.append({"assistantResponseMessage": {"content": f"assistant message {i} " + "y" * content_size}})
    return {
        "conversationState": {
            "chatTriggerType": "MANUAL",
            "conversationId": "test-conv",
            "currentMessage": {"userInputMessage": {"content": "current message", "modelId": "test"}},
            "history": history,
        },
        "profileArn": "arn:aws:test",
    }


class TestCheckPayloadSize:
    def test_check_payload_size_returns_bytes(self):
        """Correct byte count for a simple payload."""
        payload = {"key": "value"}
        expected = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        assert check_payload_size(payload) == expected

    def test_check_payload_size_matches_the_wire_encoding(self):
        """The guard must measure what the routes actually send.

        routes_openai.py and routes_anthropic.py serialize the upstream body with
        ensure_ascii=False. Measuring with the default ensure_ascii=True counts a
        Hangul character as the 6 bytes of a \\uXXXX escape instead of the 3 bytes
        UTF-8 puts on the wire, which rejected Korean conversations at roughly
        half the size Kiro accepts.
        """
        payload = {
            "content": "\uac8c\uc774\ud2b8\uc6e8\uc774\ub294 \uc5c5\uc2a4\ud2b8\ub9bc\uc774 \ubcf4\uace0\ud569\ub2c8\ub2e4"
        }

        wire = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        escaped = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

        assert escaped > wire, "fixture must actually distinguish the two encodings"
        assert check_payload_size(payload) == wire

    def test_check_payload_size_utf8(self):
        """Non-ASCII characters counted as their real UTF-8 width."""
        payload = {"emoji": "\U0001f600", "chinese": "\u4f60\u597d"}
        size = check_payload_size(payload)
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assert size == len(raw)
        assert size > 0


class TestTrimPayloadToLimit:
    def test_trim_does_nothing_when_under_limit(self):
        """No-op when payload is small."""
        payload = _make_payload(num_pairs=2, content_size=10)
        original_size = check_payload_size(payload)
        stats = trim_payload_to_limit(payload, max_bytes=original_size + 1000)
        assert not stats.trimmed
        assert stats.original_entries == stats.final_entries
        assert stats.original_bytes == stats.final_bytes

    def test_trim_removes_oldest_history_pairs(self):
        """Removes pairs from the beginning of history."""
        payload = _make_payload(num_pairs=10, content_size=500)
        original_size = check_payload_size(payload)
        # Set limit to ~half the original size
        limit = original_size // 2
        stats = trim_payload_to_limit(payload, max_bytes=limit)

        assert stats.trimmed
        assert stats.final_entries < stats.original_entries
        assert stats.final_bytes <= limit
        # History should still exist and be shorter
        history = payload["conversationState"]["history"]
        assert len(history) == stats.final_entries
        assert len(history) < 20  # original was 20 entries (10 pairs)

    def test_trim_aligns_to_user_message(self):
        """Start index always lands on userInputMessage entry."""
        payload = _make_payload(num_pairs=10, content_size=500)
        limit = check_payload_size(payload) // 3
        trim_payload_to_limit(payload, max_bytes=limit)

        history = payload["conversationState"]["history"]
        assert len(history) > 0
        assert "userInputMessage" in history[0]

    def test_trim_removes_last_oversized_history_pair(self):
        """Drops the final history pair when it alone exceeds the limit."""
        payload = _make_payload(num_pairs=5, content_size=1000)
        # Set an impossibly low limit
        stats = trim_payload_to_limit(payload, max_bytes=100)

        assert "history" not in payload["conversationState"]
        assert stats.final_entries == 0

    def test_trim_repairs_current_tool_result_after_removing_its_tool_use(self):
        """Converts a current tool result to text when trimming removes its tool use."""
        payload = {
            "conversationState": {
                "conversationId": "test",
                "chatTriggerType": "MANUAL",
                "currentMessage": {
                    "userInputMessage": {
                        "content": "",
                        "modelId": "m",
                        "userInputMessageContext": {
                            "toolResults": [
                                {
                                    "toolUseId": "tool-A",
                                    "content": [{"text": "result from tool-A"}],
                                }
                            ]
                        },
                    }
                },
                "history": [
                    {"userInputMessage": {"content": "x" * 3000}},
                    {
                        "assistantResponseMessage": {
                            "content": "using tool",
                            "toolUses": [{"toolUseId": "tool-A", "name": "read", "input": {}}],
                        }
                    },
                ],
            }
        }

        stats = trim_payload_to_limit(payload, max_bytes=1000)

        current = payload["conversationState"]["currentMessage"]["userInputMessage"]
        assert stats.final_entries == 0
        assert "history" not in payload["conversationState"]
        assert "toolResults" not in current.get("userInputMessageContext", {})
        assert "[trimmed tool result] result from tool-A" in current["content"]

    def test_trim_repairs_orphaned_tool_results(self):
        """Orphaned toolResults removed, text preserved inline."""
        history = [
            {"userInputMessage": {"content": "msg0"}},
            {
                "assistantResponseMessage": {
                    "content": "resp0",
                    "toolUses": [{"toolUseId": "tool-A", "name": "read", "input": "{}"}],
                }
            },
            {
                "userInputMessage": {
                    "content": "msg1",
                    "userInputMessageContext": {
                        "toolResults": [
                            {
                                "toolUseId": "tool-A",
                                "content": [{"text": "result from tool-A"}],
                            },
                            {
                                "toolUseId": "tool-ORPHAN",
                                "content": [{"text": "orphaned data"}],
                            },
                        ]
                    },
                }
            },
            {"assistantResponseMessage": {"content": "resp1"}},
        ]
        payload = {
            "conversationState": {
                "conversationId": "test",
                "chatTriggerType": "MANUAL",
                "currentMessage": {"userInputMessage": {"content": "now", "modelId": "m"}},
                "history": history,
            }
        }
        # Trim with a generous limit so only repair logic runs (no pair removal)
        big_limit = check_payload_size(payload) + 10000
        trim_payload_to_limit(payload, max_bytes=big_limit)

        # tool-A should remain, tool-ORPHAN should be removed with text preserved
        ctx = history[2]["userInputMessage"]["userInputMessageContext"]
        assert len(ctx["toolResults"]) == 1
        assert ctx["toolResults"][0]["toolUseId"] == "tool-A"
        # Orphaned text preserved in content
        assert "orphaned data" in history[2]["userInputMessage"]["content"]
        assert "[trimmed tool result]" in history[2]["userInputMessage"]["content"]

    def test_trim_strips_empty_tool_uses(self):
        """Empty toolUses: [] arrays cleaned before size measurement."""
        history = [
            {"userInputMessage": {"content": "msg"}},
            {"assistantResponseMessage": {"content": "resp", "toolUses": []}},
        ]
        payload = {
            "conversationState": {
                "conversationId": "test",
                "chatTriggerType": "MANUAL",
                "currentMessage": {"userInputMessage": {"content": "now", "modelId": "m"}},
                "history": history,
            }
        }
        big_limit = check_payload_size(payload) + 10000
        trim_payload_to_limit(payload, max_bytes=big_limit)

        # Empty toolUses should be stripped
        assert "toolUses" not in history[1]["assistantResponseMessage"]

    def test_trim_stats_accurate(self):
        """Stats reflect actual changes."""
        payload = _make_payload(num_pairs=8, content_size=500)
        original_size = check_payload_size(payload)
        limit = original_size // 2

        stats = trim_payload_to_limit(payload, max_bytes=limit)

        assert stats.original_bytes == original_size
        assert stats.original_entries == 16  # 8 pairs * 2
        assert stats.final_bytes == check_payload_size(payload)
        assert stats.final_entries == len(payload["conversationState"]["history"])
        assert stats.trimmed is True

    def test_trim_no_history(self):
        """Payload with no history returns no-op stats."""
        payload = {
            "conversationState": {
                "conversationId": "test",
                "chatTriggerType": "MANUAL",
                "currentMessage": {"userInputMessage": {"content": "hi", "modelId": "m"}},
            }
        }
        stats = trim_payload_to_limit(payload, max_bytes=100)
        assert not stats.trimmed
        assert stats.original_entries == 0
        assert stats.final_entries == 0


class TestOversizedPayloadWithoutAutoTrim:
    """The guard must refuse an oversized payload instead of sending it blind.

    config.py documents "When false, returns a clear error instead of trimming",
    but the branch was never written: build_kiro_payload only acted when
    AUTO_TRIM_PAYLOAD was true, so the default configuration shipped a payload
    Kiro answers with a cryptic 400 that names no size.
    """

    def _oversized_request(self):
        from kiro.converters_core import UnifiedMessage

        # One message big enough to clear any sane byte limit on its own.
        return [UnifiedMessage(role="user", content="x" * 5000)]

    def test_raises_payload_too_large_when_trim_disabled(self, monkeypatch):
        import kiro.converters_core as cc
        from kiro.payload_guards import PayloadTooLargeError

        monkeypatch.setattr(cc, "AUTO_TRIM_PAYLOAD", False)
        monkeypatch.setattr(cc, "KIRO_MAX_PAYLOAD_BYTES", 1000)

        with pytest.raises(PayloadTooLargeError) as exc_info:
            cc.build_kiro_payload(
                messages=self._oversized_request(),
                system_prompt="",
                model_id="auto",
                tools=None,
                conversation_id="conv-oversized",
                profile_arn=None,
            )

        error = exc_info.value
        # The whole point is actionability: the operator must learn both numbers.
        assert error.payload_bytes > 1000
        assert error.limit_bytes == 1000
        assert str(error.payload_bytes) in str(error)
        assert str(error.limit_bytes) in str(error)

    def test_irreducibly_large_current_message_raises_when_trim_enabled(self, monkeypatch):
        import kiro.converters_core as cc
        from kiro.payload_guards import PayloadTooLargeError

        monkeypatch.setattr(cc, "AUTO_TRIM_PAYLOAD", True)
        monkeypatch.setattr(cc, "KIRO_MAX_PAYLOAD_BYTES", 1000)

        with pytest.raises(PayloadTooLargeError):
            cc.build_kiro_payload(
                messages=self._oversized_request(),
                system_prompt="",
                model_id="auto",
                tools=None,
                conversation_id="conv-oversized",
                profile_arn=None,
            )

    def test_reducible_history_is_trimmed_successfully(self, monkeypatch):
        import kiro.converters_core as cc

        monkeypatch.setattr(cc, "AUTO_TRIM_PAYLOAD", True)
        monkeypatch.setattr(cc, "KIRO_MAX_PAYLOAD_BYTES", 1400)
        messages = [
            cc.UnifiedMessage(role="user", content="old " + "x" * 2000),
            cc.UnifiedMessage(role="assistant", content="old answer"),
            cc.UnifiedMessage(role="user", content="recent question"),
            cc.UnifiedMessage(role="assistant", content="recent answer"),
            cc.UnifiedMessage(role="user", content="current"),
        ]
        result = cc.build_kiro_payload(messages, "", "auto", None, "conv-trim", None)

        history = result.payload["conversationState"]["history"]
        assert len(history) == 2
        assert history[0]["userInputMessage"]["content"] == "recent question"
        assert result.payload["conversationState"]["currentMessage"]["userInputMessage"]["content"] == "current"

    def test_last_oversized_history_pair_is_removed_by_builder(self, monkeypatch):
        import kiro.converters_core as cc

        monkeypatch.setattr(cc, "AUTO_TRIM_PAYLOAD", True)
        monkeypatch.setattr(cc, "KIRO_MAX_PAYLOAD_BYTES", 1000)
        messages = [
            cc.UnifiedMessage(role="user", content="old " + "x" * 3000),
            cc.UnifiedMessage(role="assistant", content="old answer"),
            cc.UnifiedMessage(role="user", content="current"),
        ]

        result = cc.build_kiro_payload(messages, "", "auto", None, "conv-full-trim", None)

        conversation_state = result.payload["conversationState"]
        assert "history" not in conversation_state
        assert conversation_state["currentMessage"]["userInputMessage"]["content"] == "current"

    def test_under_limit_payload_is_untouched_when_trim_disabled(self, monkeypatch):
        import kiro.converters_core as cc
        from kiro.payload_guards import check_payload_size

        monkeypatch.setattr(cc, "AUTO_TRIM_PAYLOAD", False)
        monkeypatch.setattr(cc, "KIRO_MAX_PAYLOAD_BYTES", 1085435)

        result = cc.build_kiro_payload(
            messages=[cc.UnifiedMessage(role="user", content="hello")],
            system_prompt="",
            model_id="auto",
            tools=None,
            conversation_id="conv-small",
            profile_arn=None,
        )

        assert check_payload_size(result.payload) < 1085435


class TestOversizedPayloadReturns400:
    """An oversized request is the caller's to fix, so it must be a 400, not a 500.

    PayloadTooLargeError is raised from build_kiro_payload deep inside the route.
    Without explicit handling it lands in the generic `except Exception` branch and
    the client sees an opaque 500 that hides the actionable byte counts.
    """

    def _payload(self, model="claude-sonnet-4.5"):
        return {"model": model, "messages": [{"role": "user", "content": "x" * 4000}]}

    def test_openai_route_returns_400_naming_the_size(self, test_client, valid_proxy_api_key, monkeypatch):
        import kiro.converters_core as cc

        monkeypatch.setattr(cc, "AUTO_TRIM_PAYLOAD", False)
        monkeypatch.setattr(cc, "KIRO_MAX_PAYLOAD_BYTES", 500)

        response = test_client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
            json=self._payload(),
        )

        assert response.status_code == 400, response.text
        body = response.text
        assert "500" in body  # the configured limit
        assert "AUTO_TRIM_PAYLOAD" in body

    def test_anthropic_route_returns_400_naming_the_size(self, test_client, valid_proxy_api_key, monkeypatch):
        import kiro.converters_core as cc

        monkeypatch.setattr(cc, "AUTO_TRIM_PAYLOAD", False)
        monkeypatch.setattr(cc, "KIRO_MAX_PAYLOAD_BYTES", 500)

        response = test_client.post(
            "/v1/messages",
            headers={"x-api-key": valid_proxy_api_key},
            json={
                "model": "claude-sonnet-4.5",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "x" * 4000}],
            },
        )

        assert response.status_code == 400, response.text
        assert "AUTO_TRIM_PAYLOAD" in response.text


class TestTokenUnitMatchesUpstream:
    """CONTENT_LENGTH_EXCEEDS_THRESHOLD tracks tokenizer units, not UTF-8 bytes.

    Measured 2026-08-23 on runtime.us-east-1.kiro.dev / generateAssistantResponse
    / claude-haiku-4.5 (no tools). Hangul JSON of 195_000 chars returned 200;
    200_000 chars returned 400. Repeated ASCII 'x' passed at 1_550_000 chars
    (~193_750 cl100k tokens) and failed at 1_575_000. Cycling latin of
    1_550_000 chars failed, so the unit is not wire bytes or Unicode scalars.
    """

    def test_hangul_is_one_cl100k_token_per_syllable(self):
        payload = {"content": "가" * 1000}
        assert check_payload_tokens(payload) >= 1000
        # UTF-8 bytes are ~3x; the old guard used that and let 250k Hangul
        # through (~750 KiB) only for upstream to 400 with no numbers.
        assert check_payload_size(payload) > check_payload_tokens(payload)

    def test_repeated_ascii_compresses_in_cl100k(self):
        payload = {"content": "x" * 8000}
        # cl100k encodes long runs of 'x' at 8 chars/token.
        assert check_payload_tokens(payload) == pytest.approx(1000, abs=50)

    def test_astral_emoji_is_two_cl100k_tokens(self):
        payload = {"content": "😀" * 100}
        assert check_payload_tokens(payload) == pytest.approx(200, abs=20)


class TestKoreanUnderOldByteCapIsRejectedByTokens:
    """800k is the measured opus-5 last-pass; 1M Hangul is a size-reject."""

    def test_korean_over_800k_raises_when_byte_cap_disabled(self, monkeypatch):
        import kiro.converters_core as cc
        from kiro.converters_core import UnifiedMessage
        from kiro.payload_guards import PayloadTooLargeError

        monkeypatch.setattr(cc, "AUTO_TRIM_PAYLOAD", False)
        monkeypatch.setattr(cc, "KIRO_MAX_PAYLOAD_BYTES", 0)

        with pytest.raises(PayloadTooLargeError) as exc_info:
            cc.build_kiro_payload(
                messages=[UnifiedMessage(role="user", content="가" * 900000)],
                system_prompt="",
                model_id="claude-opus-5",
                tools=None,
                conversation_id="conv-korean-over-tokens",
                profile_arn=None,
            )

        error = exc_info.value
        assert "token" in str(error).lower()
        assert "800000" in str(error)
        assert error.limit_tokens == 800000
        assert error.payload_tokens > 800000

    def test_korean_under_token_limit_does_not_raise(self, monkeypatch):
        import kiro.converters_core as cc
        from kiro.converters_core import UnifiedMessage

        monkeypatch.setattr(cc, "AUTO_TRIM_PAYLOAD", False)
        monkeypatch.setattr(cc, "KIRO_MAX_PAYLOAD_BYTES", 1085435)

        result = cc.build_kiro_payload(
            messages=[UnifiedMessage(role="user", content="가" * 1000)],
            system_prompt="",
            model_id="auto",
            tools=None,
            conversation_id="conv-korean-under-tokens",
            profile_arn=None,
        )
        assert "가" * 1000 in result.payload["conversationState"]["currentMessage"]["userInputMessage"]["content"]

    def test_trim_uses_tokens_for_hangul_history(self):
        history = []
        for i in range(25):
            history.append({"userInputMessage": {"content": "가" * 40000}})
            history.append({"assistantResponseMessage": {"content": "나" * 1000}})
        payload = {
            "conversationState": {
                "chatTriggerType": "MANUAL",
                "conversationId": "test-conv",
                "currentMessage": {"userInputMessage": {"content": "현재", "modelId": "test"}},
                "history": history,
            }
        }
        stats = trim_payload_to_limit(payload, max_tokens=800000)
        assert stats.trimmed
        assert stats.final_tokens <= 800000
        assert "userInputMessage" in payload["conversationState"]["history"][0]

    def test_legacy_byte_cap_still_honored(self, monkeypatch):
        import kiro.converters_core as cc
        from kiro.converters_core import UnifiedMessage
        from kiro.payload_guards import PayloadTooLargeError

        monkeypatch.setattr(cc, "AUTO_TRIM_PAYLOAD", False)
        monkeypatch.setattr(cc, "KIRO_MAX_PAYLOAD_BYTES", 1000)

        with pytest.raises(PayloadTooLargeError) as exc_info:
            cc.build_kiro_payload(
                messages=[UnifiedMessage(role="user", content="x" * 5000)],
                system_prompt="",
                model_id="auto",
                tools=None,
                conversation_id="conv-legacy-bytes",
                profile_arn=None,
            )
        assert exc_info.value.limit_bytes == 1000
        assert "byte" in str(exc_info.value).lower()

    def test_cap_is_800k_for_haiku_and_opus(self):
        assert payload_token_limit_for_model("claude-haiku-4.5") == 800000
        assert payload_token_limit_for_model("claude-opus-5") == 800000

    def test_opus5_allows_250k_hangul(self, monkeypatch):
        import kiro.converters_core as cc
        from kiro.converters_core import UnifiedMessage

        monkeypatch.setattr(cc, "AUTO_TRIM_PAYLOAD", False)
        monkeypatch.setattr(cc, "KIRO_MAX_PAYLOAD_BYTES", 0)

        result = cc.build_kiro_payload(
            messages=[UnifiedMessage(role="user", content="가" * 250000)],
            system_prompt="",
            model_id="claude-opus-5",
            tools=None,
            conversation_id="conv-opus-250k",
            profile_arn=None,
        )
        content = result.payload["conversationState"]["currentMessage"]["userInputMessage"]["content"]
        assert content == "가" * 250000
