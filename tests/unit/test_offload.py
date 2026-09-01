# -*- coding: utf-8 -*-
"""CPU-bound payload conversion must not stall the asyncio event loop."""

from __future__ import annotations

import asyncio
import threading

from kiro.converters_anthropic import anthropic_to_kiro_with_stats
from kiro.converters_openai import build_kiro_payload
from kiro.offload import run_in_worker
from kiro.payload_guards import PayloadTooLargeError


class TestRunInWorker:
    def test_event_loop_progresses_while_worker_runs(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def block() -> str:
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("release not signaled")
            return "done"

        loop = asyncio.new_event_loop()

        async def main() -> str:
            return await run_in_worker(block)

        def runner() -> None:
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(main())
            finally:
                loop.close()

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        assert started.wait(timeout=2), "worker never started"
        progressed = asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop)
        try:
            progressed.result(timeout=1)
        except Exception as exc:
            release.set()
            raise AssertionError("event loop did not progress while worker ran") from exc
        release.set()
        thread.join(timeout=2)
        assert not thread.is_alive()


class TestRoutesOffloadPayloadBuild:
    def test_anthropic_messages_offloads_conversion(self, test_client, valid_proxy_api_key, monkeypatch) -> None:
        import kiro.routes_anthropic as routes

        seen: dict[str, object] = {}

        async def fake(fn, *args, **kwargs):
            seen["fn"] = fn
            raise PayloadTooLargeError(1, 1, unit="tokens")

        monkeypatch.setattr(routes, "run_in_worker", fake)
        response = test_client.post(
            "/v1/messages",
            headers={"x-api-key": valid_proxy_api_key},
            json={
                "model": "claude-sonnet-4.5",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert seen.get("fn") is anthropic_to_kiro_with_stats
        assert response.status_code == 400

    def test_openai_chat_offloads_conversion(self, test_client, valid_proxy_api_key, monkeypatch) -> None:
        import kiro.routes_openai as routes

        seen: dict[str, object] = {}

        async def fake(fn, *args, **kwargs):
            seen["fn"] = fn
            raise PayloadTooLargeError(1, 1, unit="tokens")

        monkeypatch.setattr(routes, "run_in_worker", fake)
        response = test_client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
            json={"model": "claude-sonnet-4.5", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert seen.get("fn") is build_kiro_payload
        assert response.status_code == 400
