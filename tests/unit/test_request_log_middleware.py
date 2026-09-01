# -*- coding: utf-8 -*-
"""The request log middleware must not break the data plane.

A missing import in the tee reached production as a 500 on every streamed
request, so the streaming path is exercised here with capture enabled.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_ENCRYPTION_KEY", Fernet.generate_key().decode())

    from kiro import dashboard, gateway_tunables, log_crypto

    log_crypto.reset_cache()
    dashboard.initialize_dashboard_store()
    gateway_tunables.reset_all()
    gateway_tunables.CAPTURE_TEXT.set(True)

    import main

    probe = FastAPI()
    probe.middleware("http")(main.dashboard_request_metrics)

    @probe.post("/v1/messages")
    async def messages():
        async def body():
            yield b'data: {"type":"message_start","message":{"usage":{"input_tokens":11}}}\n\n'
            yield b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}\n\n'
            yield b'data: {"type":"message_delta","usage":{"output_tokens":2},"creditUsage":0.03}\n\n'

        return StreamingResponse(body(), media_type="text/event-stream")

    @probe.post("/v1/plain")
    async def plain():
        return {"ok": True}

    yield probe, dashboard
    gateway_tunables.reset_all()
    log_crypto.reset_cache()


def _logs(dashboard):
    with dashboard._db() as conn:
        return conn.execute(
            "SELECT status_code, input_tokens, output_tokens, credits, response_enc FROM request_logs ORDER BY id"
        ).fetchall()


class TestStreamingCapture:
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

    def test_response_text_is_stored_encrypted(self, app):
        probe, dashboard = app
        from kiro import log_crypto

        with TestClient(probe) as client:
            client.post("/v1/messages", json={"model": "claude-opus-5", "messages": []})
        blob = _logs(dashboard)[0]["response_enc"]
        assert blob is not None
        assert b"text_delta" not in bytes(blob), "stored text must not be readable in the database"
        assert log_crypto.decrypt(blob) == "ok"

    def test_non_streamed_response_still_records(self, app):
        probe, dashboard = app
        with TestClient(probe) as client:
            assert client.post("/v1/plain", json={"model": "auto", "messages": []}).status_code == 200
        assert len(_logs(dashboard)) == 1

    def test_capture_off_stores_no_text(self, app, monkeypatch):
        probe, dashboard = app
        from kiro import gateway_tunables

        gateway_tunables.CAPTURE_TEXT.set(False)
        with TestClient(probe) as client:
            assert client.post("/v1/messages", json={"model": "auto", "messages": []}).status_code == 200
        assert _logs(dashboard)[0]["response_enc"] is None
