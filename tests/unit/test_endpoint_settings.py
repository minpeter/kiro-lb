# -*- coding: utf-8 -*-
"""Endpoint settings, the prompt filter, and the dashboard routes that expose them."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kiro import agent_mode, endpoint_settings, prompt_filter, store


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    store.initialize()
    endpoint_settings.reset_cache()
    prompt_filter.reset_cache()
    agent_mode.reset_cache()
    yield
    endpoint_settings.reset_cache()
    prompt_filter.reset_cache()
    agent_mode.reset_cache()


class TestEndpointSettingsValidation:
    def test_rejects_an_empty_order(self):
        """An empty order would leave generation with nowhere to go."""
        with pytest.raises(endpoint_settings.InvalidEndpointSettings, match="at least one"):
            endpoint_settings.validate(True, [], 30)

    def test_rejects_an_unknown_key(self):
        with pytest.raises(endpoint_settings.InvalidEndpointSettings, match="unknown endpoint keys"):
            endpoint_settings.validate(True, ["runtime", "made-up"], 30)

    def test_rejects_a_string_instead_of_a_list(self):
        with pytest.raises(endpoint_settings.InvalidEndpointSettings, match="must be a list"):
            endpoint_settings.validate(True, "runtime", 30)

    def test_rejects_a_non_boolean_rotation(self):
        with pytest.raises(endpoint_settings.InvalidEndpointSettings, match="must be a boolean"):
            endpoint_settings.validate("yes", ["runtime"], 30)

    @pytest.mark.parametrize("cooldown", [-1, 3601, "abc"])
    def test_rejects_a_cooldown_outside_the_range(self, cooldown):
        with pytest.raises(endpoint_settings.InvalidEndpointSettings):
            endpoint_settings.validate(True, ["runtime"], cooldown)

    def test_deduplicates_while_keeping_priority(self):
        settings = endpoint_settings.validate(True, ["amazonq", "runtime", "amazonq"], 15)
        assert settings.order == ("amazonq", "runtime")


class TestEndpointSettingsPersistence:
    def test_update_survives_a_cache_reset(self):
        endpoint_settings.update(True, ["codewhisperer", "runtime"], 12)
        endpoint_settings.reset_cache()
        reloaded = endpoint_settings.load_from_store()
        assert reloaded.order == ("codewhisperer", "runtime")
        assert reloaded.cooldown_seconds == 12

    def test_a_corrupt_row_falls_back_to_the_environment(self):
        store.save_setting(endpoint_settings.SETTING_KEY, {"rotation": True, "order": ["bogus"], "cooldownSeconds": 5})
        endpoint_settings.reset_cache()
        reloaded = endpoint_settings.load_from_store()
        assert all(key in endpoint_settings.ENDPOINTS_BY_KEY for key in reloaded.order)
        assert reloaded.order

    def test_current_is_populated_without_a_stored_row(self):
        assert endpoint_settings.current().order


class TestPromptFilterDetection:
    def _builtin(self) -> str:
        return (
            "You are an interactive agent that helps users with software engineering tasks.\n\n"
            "# Harness\n - Output renders as markdown.\n\n"
            "# Delivering work\nDo ordinary work as asked.\n\n"
            "# Memory\nYour memory lives at C:/somewhere/memory/.\n\n"
            "# Environment\n - Primary working directory: C:/project\n\n"
            "# Language\nAlways respond in Portuguese.\n" + "padding. " * 200
        )

    def test_detects_the_builtin_prompt(self):
        assert prompt_filter.is_claude_code_prompt(self._builtin())

    def test_ignores_a_short_block_that_names_claude_code(self):
        assert not prompt_filter.is_claude_code_prompt("I am using Claude Code today.")

    def test_ignores_user_rules_of_similar_size(self):
        rules = "# Global Rules\n\n1. Write code without comments\n2. Answer in pt-br\n" * 60
        assert not prompt_filter.is_claude_code_prompt(rules)

    def test_condense_keeps_the_user_and_machine_sections(self):
        condensed = prompt_filter.condense(self._builtin())
        assert "# Memory" in condensed
        assert "C:/somewhere/memory/" in condensed
        assert "# Environment" in condensed
        assert "C:/project" in condensed
        assert "# Language" in condensed

    def test_condense_drops_the_generic_prose(self):
        condensed = prompt_filter.condense(self._builtin())
        assert "Do ordinary work as asked" not in condensed

    def test_condense_states_it_is_kiro(self):
        assert "You are Kiro" in prompt_filter.condense(self._builtin())

    def test_condense_preserves_an_unrecognised_section(self):
        """Failing toward keeping content: a future section must survive."""
        text = self._builtin() + "\n\n# Newly Added Section\nSomething important.\n"
        condensed = prompt_filter.condense(text)
        assert "# Newly Added Section" in condensed
        assert "Something important." in condensed

    def test_identity_block_becomes_kiro(self):
        blocks = ["You are a Claude agent, built on Anthropic's Claude Agent SDK."]
        filtered, stats = prompt_filter.filter_blocks(blocks)
        assert filtered[0] == prompt_filter.KIRO_IDENTITY
        assert stats["blocksCondensed"] == 1

    def test_unrelated_blocks_pass_through(self):
        blocks = ["x-anthropic-billing-header: cc_version=1;", "My own project rules."]
        filtered, stats = prompt_filter.filter_blocks(blocks)
        assert filtered == blocks
        assert stats["blocksCondensed"] == 0


class TestPromptFilterToggle:
    def test_defaults_to_off(self):
        assert prompt_filter.enabled() is False

    def test_set_enabled_persists(self):
        prompt_filter.set_enabled(True)
        prompt_filter.reset_cache()
        assert prompt_filter.load_from_store() is True

    def test_rejects_a_non_boolean(self):
        with pytest.raises(ValueError):
            prompt_filter.set_enabled("yes")

    def test_converter_is_untouched_while_disabled(self):
        from kiro.converters_anthropic import extract_system_prompt

        blocks = [{"type": "text", "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK."}]
        assert extract_system_prompt(blocks) == blocks[0]["text"]

    def test_converter_condenses_once_enabled(self):
        from kiro.converters_anthropic import extract_system_prompt

        prompt_filter.set_enabled(True)
        blocks = [{"type": "text", "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK."}]
        assert extract_system_prompt(blocks) == prompt_filter.KIRO_IDENTITY


class TestAgentMode:
    def test_defaults_to_vibe(self):
        assert agent_mode.current() == "vibe"

    @pytest.mark.parametrize("mode", ["vibe", "spec", "task", ""])
    def test_accepts_the_known_modes(self, mode):
        assert agent_mode.validate(mode) == mode

    def test_rejects_an_unknown_mode(self):
        with pytest.raises(agent_mode.InvalidAgentMode, match="unknown mode"):
            agent_mode.validate("turbo")

    def test_rejects_a_non_string(self):
        with pytest.raises(agent_mode.InvalidAgentMode, match="must be a string"):
            agent_mode.validate(7)

    def test_set_mode_persists(self):
        agent_mode.set_mode("spec")
        agent_mode.reset_cache()
        assert agent_mode.load_from_store() == "spec"

    def test_a_corrupt_row_falls_back_to_the_default(self):
        store.save_setting(agent_mode.SETTING_KEY, "nonsense")
        agent_mode.reset_cache()
        assert agent_mode.load_from_store() == "vibe"

    def _payload(self):
        from kiro.converters_core import UnifiedMessage, build_kiro_payload

        return build_kiro_payload(
            messages=[UnifiedMessage(role="user", content="oi")],
            system_prompt="",
            model_id="claude-sonnet-4.5",
            tools=None,
            conversation_id="c-1",
            profile_arn="arn:test",
        ).payload

    def test_payload_carries_the_mode(self):
        assert self._payload()["conversationState"]["agentTaskType"] == "vibe"

    def test_empty_mode_omits_the_field(self):
        agent_mode.set_mode("")
        assert "agentTaskType" not in self._payload()["conversationState"]

    def test_mode_change_applies_without_a_restart(self):
        agent_mode.set_mode("spec")
        assert self._payload()["conversationState"]["agentTaskType"] == "spec"


class TestRoutesRequireAuth:
    @pytest.fixture
    def client(self, monkeypatch):
        """Pin the session to unauthenticated.

        Other suites replace ``dashboard._authenticated`` by direct assignment,
        which leaks for the rest of the session. Setting it here through
        monkeypatch keeps this test hermetic and states the property under test:
        every route below must call the guard.
        """
        from kiro import dashboard

        monkeypatch.setattr(dashboard, "_authenticated", lambda _request: False)
        app = FastAPI()
        app.include_router(dashboard.router)
        return TestClient(app)

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/api/dashboard/endpoints"),
            ("PUT", "/api/dashboard/endpoints"),
            ("POST", "/api/dashboard/endpoints/test"),
            ("POST", "/api/dashboard/endpoints/ping"),
            ("GET", "/api/dashboard/prompt-filter"),
            ("PUT", "/api/dashboard/prompt-filter"),
            ("GET", "/api/dashboard/agent-mode"),
            ("PUT", "/api/dashboard/agent-mode"),
        ],
    )
    def test_unauthenticated_is_rejected(self, client, method, path):
        assert client.request(method, path, json={}).status_code == 401


class TestProbeGuards:
    @pytest.mark.asyncio
    async def test_a_second_probe_is_refused(self):
        """Two clicks must not spend quota twice over."""
        from kiro import endpoint_probe

        await endpoint_probe._probe_lock.acquire()
        try:
            with pytest.raises(endpoint_probe.ProbeBusy):
                await endpoint_probe.test_endpoints(object())
            with pytest.raises(endpoint_probe.ProbeBusy):
                await endpoint_probe.ping_endpoints(object())
        finally:
            endpoint_probe._probe_lock.release()

    def test_verdict_refuses_to_rank_inside_the_noise(self):
        from kiro.endpoint_probe import _verdict

        verdict = _verdict({"a": [1000.0, 3000.0], "b": [1100.0, 1200.0]})
        assert verdict["conclusive"] is False
        assert "Indistinguishable" in verdict["verdict"]

    def test_verdict_reports_a_gap_above_the_noise(self):
        from kiro.endpoint_probe import _verdict

        verdict = _verdict({"a": [1000.0, 1010.0], "b": [5000.0, 5010.0]})
        assert verdict["conclusive"] is True
        assert verdict["fastest"] == "a"

    def test_verdict_handles_nothing_answering(self):
        from kiro.endpoint_probe import _verdict

        verdict = _verdict({"a": [], "b": []})
        assert verdict["fastest"] is None
        assert verdict["conclusive"] is False
