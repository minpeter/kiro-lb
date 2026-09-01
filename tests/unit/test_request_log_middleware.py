# -*- coding: utf-8 -*-
"""The request log middleware must not break the data plane.

A missing import in the stream relay once reached production as a 500 on every
streamed request, so the streaming path is exercised end to end. The log holds
metadata only: token counts and credits arrive through the usage ContextVar
that the streaming layer fills, never by reading the response body.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from kiro.usage_tracking import current_api_key_id, record_token_usage, report_credits


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))

    from kiro import dashboard, gateway_tunables

    dashboard.initialize_dashboard_store()
    gateway_tunables.reset_all()

    import main

    probe = FastAPI()
    probe.middleware("http")(main.dashboard_request_metrics)

    @probe.post("/v1/messages")
    async def messages():
        # Emulates the streaming layer: the usage frame arrives at the end of
        # the body, long after the middleware has returned.
        async def body():
            yield b'data: {"type":"message_start"}\n\n'
            yield b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}\n\n'
            current_api_key_id.set("test-key")
            record_token_usage("claude-opus-5", 11, 2)
            report_credits(0.03)
            yield b'data: {"type":"message_stop"}\n\n'

        return StreamingResponse(body(), media_type="text/event-stream")

    @probe.post("/v1/wrapped-credits")
    async def wrapped_credits():
        async def body():
            report_credits({"creditUsage": 0.05})
            yield b"data: ok\n\n"

        return StreamingResponse(body(), media_type="text/event-stream")

    @probe.post("/v1/plain")
    async def plain():
        return {"ok": True}

    yield probe, dashboard
    gateway_tunables.reset_all()


def _logs(dashboard):
    with dashboard._db() as conn:
        return conn.execute(
            "SELECT status_code, input_tokens, output_tokens, credits FROM request_logs ORDER BY id"
        ).fetchall()


class TestStreamingRequestLog:
    def test_streamed_body_reaches_the_client_intact(self, app):
        probe, _ = app
        with TestClient(probe) as client:
            response = client.post("/v1/messages", json={"model": "claude-opus-5", "messages": []})
        assert response.status_code == 200
        assert "message_start" in response.text
        assert "text_delta" in response.text

    def test_usage_and_credits_are_recorded(self, app):
        probe, dashboard = app
        with TestClient(probe) as client:
            client.post("/v1/messages", json={"model": "claude-opus-5", "messages": []})
        rows = _logs(dashboard)
        assert len(rows) == 1
        assert rows[0]["status_code"] == 200
        assert rows[0]["input_tokens"] == 11
        assert rows[0]["output_tokens"] == 2
        assert rows[0]["credits"] == pytest.approx(0.03)

    def test_wrapped_credit_frame_is_unwrapped(self, app):
        probe, dashboard = app
        with TestClient(probe) as client:
            client.post("/v1/wrapped-credits", json={"model": "auto", "messages": []})
        assert _logs(dashboard)[0]["credits"] == pytest.approx(0.05)

    def test_non_streamed_response_still_records(self, app):
        probe, dashboard = app
        with TestClient(probe) as client:
            assert client.post("/v1/plain", json={"model": "auto", "messages": []}).status_code == 200
        assert len(_logs(dashboard)) == 1

    def test_store_has_no_text_columns(self, app):
        _, dashboard = app
        with dashboard._db() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(request_logs)")}
        assert not columns & {"prompt_enc", "system_enc", "response_enc"}, (
            "the request log must never grow a column that holds request or response text"
        )
