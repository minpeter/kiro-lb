"""Per-key, per-model token accounting.

The key is known at authentication time but the token counts are only final
inside the serializers, so identity travels in a ContextVar. These tests pin the
attribution, the read-only root entry for the environment key, and that totals
survive a restart.
"""

import asyncio
import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro.usage_tracking import ROOT_KEY_ID, current_api_key_id, drain_pending_usage, record_token_usage


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PROXY_API_KEY", "legacy-root-secret")
    module = importlib.reload(importlib.import_module("kiro.dashboard"))
    module.initialize_dashboard_store()
    drain_pending_usage()
    current_api_key_id.set(None)
    return module


@pytest.fixture
def stream_deps():
    response = AsyncMock()
    response.status_code = 200
    response.aclose = AsyncMock()
    cache = MagicMock()
    cache.get_max_input_tokens.return_value = 200000
    return AsyncMock(), response, cache, MagicMock()


def _list_keys(dashboard):
    dashboard._authenticated = lambda _request: True
    return asyncio.run(dashboard.dashboard_list_keys(MagicMock()))["apiKeys"]


def test_legacy_key_identifies_as_root(dashboard):
    assert dashboard.identify_data_api_key("legacy-root-secret") == ROOT_KEY_ID


def test_unknown_key_identifies_as_nothing(dashboard):
    assert dashboard.identify_data_api_key("klb_notreal") is None
    assert dashboard.identify_data_api_key("garbage") is None


def test_created_key_identifies_as_its_own_id(dashboard):
    raw, metadata = dashboard.create_data_api_key("laptop")

    assert dashboard.identify_data_api_key(raw) == metadata["id"]


def test_revoked_key_stops_identifying(dashboard):
    raw, metadata = dashboard.create_data_api_key("temporary")
    dashboard.revoke_data_api_key(metadata["id"])

    assert dashboard.identify_data_api_key(raw) is None


def test_root_key_is_listed_as_read_only(dashboard):
    dashboard.create_data_api_key("laptop")

    keys = _list_keys(dashboard)
    root = next(key for key in keys if key["id"] == ROOT_KEY_ID)
    managed = next(key for key in keys if key["id"] != ROOT_KEY_ID)

    assert root["readOnly"] is True
    assert root["revokedAt"] is None
    assert managed["readOnly"] is False
    # The plaintext root secret must never reach the client, only a short hint.
    assert "legacy-root-secret" not in root["prefix"]


def test_root_key_is_hidden_when_no_legacy_key_is_set(dashboard, monkeypatch):
    monkeypatch.delenv("PROXY_API_KEY", raising=False)

    assert all(key["id"] != ROOT_KEY_ID for key in _list_keys(dashboard))


def test_root_key_cannot_be_revoked(dashboard):
    dashboard._authenticated = lambda _request: True

    with pytest.raises(Exception) as exc_info:
        asyncio.run(dashboard.dashboard_revoke_key(ROOT_KEY_ID, MagicMock()))

    assert getattr(exc_info.value, "status_code", None) == 400


def test_usage_is_attributed_to_the_calling_key(dashboard):
    _, metadata = dashboard.create_data_api_key("laptop")

    current_api_key_id.set(ROOT_KEY_ID)
    record_token_usage("claude-opus-5", 100, 50)
    record_token_usage("claude-opus-5", 10, 5)
    current_api_key_id.set(metadata["id"])
    record_token_usage("claude-opus-5", 1000, 500)

    dashboard.flush_key_model_usage()
    usage = dashboard.key_model_usage()

    root_row = usage[ROOT_KEY_ID][0]
    assert (root_row["promptTokens"], root_row["completionTokens"], root_row["requests"]) == (110, 55, 2)
    laptop_row = usage[metadata["id"]][0]
    assert (laptop_row["promptTokens"], laptop_row["completionTokens"], laptop_row["requests"]) == (1000, 500, 1)


def test_usage_is_split_per_model(dashboard):
    current_api_key_id.set(ROOT_KEY_ID)
    record_token_usage("claude-opus-5", 100, 50)
    record_token_usage("claude-sonnet-4-5", 7, 3)

    dashboard.flush_key_model_usage()
    rows = {row["model"]: row for row in dashboard.key_model_usage()[ROOT_KEY_ID]}

    assert rows["claude-opus-5"]["totalTokens"] == 150
    assert rows["claude-sonnet-4-5"]["totalTokens"] == 10


def test_usage_without_a_key_is_not_recorded(dashboard):
    current_api_key_id.set(None)
    record_token_usage("claude-opus-5", 100, 50)

    assert dashboard.flush_key_model_usage() == 0
    assert dashboard.key_model_usage() == {}


def test_usage_without_a_model_is_not_recorded(dashboard):
    current_api_key_id.set(ROOT_KEY_ID)
    record_token_usage("", 100, 50)

    assert dashboard.flush_key_model_usage() == 0


def test_totals_accumulate_across_flushes(dashboard):
    current_api_key_id.set(ROOT_KEY_ID)
    record_token_usage("claude-opus-5", 100, 50)
    dashboard.flush_key_model_usage()
    record_token_usage("claude-opus-5", 1, 2)
    dashboard.flush_key_model_usage()

    row = dashboard.key_model_usage()[ROOT_KEY_ID][0]

    assert (row["promptTokens"], row["completionTokens"], row["requests"]) == (101, 52, 2)


def test_flushing_twice_does_not_double_count(dashboard):
    current_api_key_id.set(ROOT_KEY_ID)
    record_token_usage("claude-opus-5", 100, 50)

    assert dashboard.flush_key_model_usage() == 1
    assert dashboard.flush_key_model_usage() == 0
    assert dashboard.key_model_usage()[ROOT_KEY_ID][0]["promptTokens"] == 100


def test_usage_survives_a_restart(dashboard, tmp_path, monkeypatch):
    current_api_key_id.set(ROOT_KEY_ID)
    record_token_usage("claude-opus-5", 321, 123)
    dashboard.flush_key_model_usage()

    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    restarted = importlib.reload(importlib.import_module("kiro.dashboard"))

    row = restarted.key_model_usage()[ROOT_KEY_ID][0]
    assert (row["promptTokens"], row["completionTokens"]) == (321, 123)


@pytest.mark.asyncio
async def test_openai_stream_records_usage_for_the_calling_key(dashboard, stream_deps):
    """Drive the real serializer so a missing hook fails here, not in production."""
    client, response, cache, auth = stream_deps

    from kiro.streaming_core import KiroEvent
    from kiro.streaming_openai import stream_kiro_to_openai

    async def upstream(*_args, **_kwargs):
        yield KiroEvent(type="content", content="Hello there")

    current_api_key_id.set(ROOT_KEY_ID)
    with patch("kiro.streaming_openai.parse_kiro_stream", upstream):
        with patch("kiro.streaming_openai.parse_bracket_tool_calls", return_value=[]):
            async for _chunk in stream_kiro_to_openai(client, response, "claude-sonnet-4", cache, auth):
                pass

    dashboard.flush_key_model_usage()
    row = dashboard.key_model_usage()[ROOT_KEY_ID][0]

    assert row["model"] == "claude-sonnet-4"
    assert row["requests"] == 1
    assert row["completionTokens"] > 0


@pytest.mark.asyncio
async def test_anthropic_stream_records_usage_for_the_calling_key(dashboard, stream_deps):
    _client, response, cache, auth = stream_deps

    from kiro.streaming_anthropic import stream_kiro_to_anthropic
    from kiro.streaming_core import KiroEvent

    async def upstream(*_args, **_kwargs):
        yield KiroEvent(type="content", content="Hello there")

    current_api_key_id.set(ROOT_KEY_ID)
    with patch("kiro.streaming_anthropic.parse_kiro_stream", upstream):
        with patch("kiro.streaming_anthropic.parse_bracket_tool_calls", return_value=[]):
            async for _chunk in stream_kiro_to_anthropic(response, "claude-opus-5", cache, auth):
                pass

    dashboard.flush_key_model_usage()
    row = dashboard.key_model_usage()[ROOT_KEY_ID][0]

    assert row["model"] == "claude-opus-5"
    assert row["requests"] == 1
