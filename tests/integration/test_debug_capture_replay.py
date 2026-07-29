import json
import base64
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from loguru import logger as app_logger

from kiro.debug_logger import DebugLogger
from kiro.debug_middleware import DebugLoggerMiddleware


def _debug_logger(debug_dir: Path) -> DebugLogger:
    logger = DebugLogger.__new__(DebugLogger)
    logger._initialized = False
    logger.__init__()
    logger.debug_dir = debug_dir
    return logger


def test_real_http_stream_failure_produces_replayable_capture(tmp_path: Path) -> None:
    debug_dir = tmp_path / "debug"

    with (
        patch("kiro.debug_logger.DEBUG_MODE", "errors"),
        patch("kiro.debug_logger.DEBUG_DIR", str(debug_dir)),
        patch("kiro.debug_middleware.DEBUG_MODE", "errors"),
    ):
        logger = _debug_logger(debug_dir)
        app = FastAPI()
        app.add_middleware(DebugLoggerMiddleware)

        async def invalid_stream() -> AsyncIterator[str]:
            logger.log_kiro_request_body(
                b'{"conversationState":{"currentMessage":{"content":"private"}}}'
            )
            logger.log_raw_chunk(b'{"content":"answer"}')
            chunks = [
                'event: message_start\n'
                'data: {"type":"message_start","message":{"content":[]}}\n\n'
                ,
                'event: content_block_start\n'
                'data: {"type":"content_block_start","index":0,'
                '"content_block":{"type":"text","text":""}}\n\n'
                ,
                'event: content_block_start\n'
                'data: {"type":"content_block_start","index":0,'
                '"content_block":{"type":"thinking","thinking":"","signature":""}}\n\n'
                ,
            ]
            for chunk in chunks:
                logger.log_modified_chunk(chunk.encode())
                yield chunk
            logger.flush_on_error(500, "Invalid assistant content event order")

        @app.post("/v1/messages")
        async def messages() -> StreamingResponse:
            return StreamingResponse(invalid_stream(), media_type="text/event-stream")

        with (
            patch("kiro.debug_logger.debug_logger", logger),
            TestClient(app) as client,
        ):
            response = client.post(
                "/v1/messages",
                headers={
                    "x-api-key": "probe-secret-key",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-opus-5",
                    "max_tokens": 128,
                    "messages": [
                        {"role": "user", "content": "original private prompt"}
                    ],
                },
            )

        assert response.status_code == 200
        assert "content_block_start" in response.text

        failures_dir = debug_dir / "failures"
        assert failures_dir.is_dir()
        captures = [
            path
            for path in failures_dir.iterdir()
            if path.is_dir() and not path.name.startswith(".tmp-")
        ]
        assert len(captures) == 1

        capture = captures[0]
        assert (capture / "manifest.json").is_file()
        replay = json.loads((capture / "replay.json").read_text())
        assert replay["failure"] == "Invalid assistant content event order"
        assert replay["validation"] == {
            "valid": False,
            "failure": "Invalid assistant content event order",
        }


def test_parallel_real_http_failures_produce_independent_bundles(
    tmp_path: Path,
) -> None:
    debug_dir = tmp_path / "debug"
    barrier = Barrier(2)
    request_ids: list[str | None] = []
    capture_paths: list[Path | None] = []

    with (
        patch("kiro.debug_logger.DEBUG_MODE", "errors"),
        patch("kiro.debug_logger.DEBUG_DIR", str(debug_dir)),
        patch("kiro.debug_logger.DEBUG_CAPTURE_CONTENT", True, create=True),
        patch("kiro.debug_logger.DEBUG_CAPTURE_MAX_BYTES", 65536, create=True),
        patch("kiro.debug_logger.DEBUG_CAPTURE_RETENTION", 10, create=True),
        patch("kiro.debug_middleware.DEBUG_MODE", "errors"),
    ):
        logger = _debug_logger(debug_dir)
        app = FastAPI()
        app.add_middleware(DebugLoggerMiddleware)

        @app.post("/v1/messages")
        async def messages(marker: str) -> StreamingResponse:
            async def failed_stream() -> AsyncIterator[str]:
                capture = logger._current_capture()
                request_ids.append(
                    capture.request_id if capture is not None else None
                )
                barrier.wait(timeout=2)
                logger.log_kiro_request_body(
                    json.dumps({"marker": marker}).encode()
                )
                logger.log_raw_chunk(
                    json.dumps({"content": marker}).encode()
                )
                app_logger.info(f"private-log-{marker}")
                chunk = (
                    "event: message_start\n"
                    f'data: {{"type":"message_start","marker":"{marker}"}}\n\n'
                )
                logger.log_modified_chunk(chunk.encode())
                yield chunk
                capture_paths.append(logger.flush_on_error(
                    500,
                    "Invalid assistant content event order",
                ))

            return StreamingResponse(
                failed_stream(),
                media_type="text/event-stream",
            )

        def send(marker: str) -> int:
            with TestClient(app) as client:
                response = client.post(
                    f"/v1/messages?marker={marker}",
                    json={
                        "model": "claude-opus-5",
                        "max_tokens": 128,
                        "messages": [
                            {"role": "user", "content": marker}
                        ],
                    },
                )
                return response.status_code

        with patch("kiro.debug_logger.debug_logger", logger):
            with ThreadPoolExecutor(max_workers=2) as executor:
                statuses = list(executor.map(
                    send,
                    ("request-alpha", "request-beta"),
                ))

    assert statuses == [200, 200]
    assert None not in request_ids
    assert len(set(request_ids)) == 2
    assert None not in capture_paths
    captures = sorted(
        path
        for path in (debug_dir / "failures").iterdir()
        if path.is_dir() and not path.name.startswith(".tmp-")
    )
    assert len(captures) == 2
    artifacts = [
        (
            (capture / "client_request.json").read_text(),
            (capture / "kiro_request.json").read_text(),
            (capture / "app_logs.txt").read_text(),
        )
        for capture in captures
    ]
    for marker in ("request-alpha", "request-beta"):
        matching = [
            pair
            for pair in artifacts
            if marker in pair[0]
            and marker in pair[1]
            and f"private-log-{marker}" in pair[2]
        ]
        assert len(matching) == 1
        other = (
            "request-beta"
            if marker == "request-alpha"
            else "request-alpha"
        )
        assert other not in matching[0][0]
        assert other not in matching[0][1]
        assert f"private-log-{other}" not in matching[0][2]


def test_successful_stream_retains_sanitized_rolling_capture(tmp_path: Path) -> None:
    debug_dir = tmp_path / "debug"

    with (
        patch("kiro.debug_logger.DEBUG_MODE", "errors"),
        patch("kiro.debug_logger.DEBUG_CAPTURE_SUCCESS", True),
        patch("kiro.debug_middleware.DEBUG_MODE", "errors"),
    ):
        logger = _debug_logger(debug_dir)
        app = FastAPI()
        app.add_middleware(DebugLoggerMiddleware)

        async def successful_stream() -> AsyncIterator[str]:
            logger.log_kiro_request_body(
                b'{"authorization":"Bearer upstream-secret","model":"claude-opus-5"}'
            )
            logger.log_raw_chunk(b'{"content":"private upstream output"}')
            logger.log_parsed_event({
                "type": "content",
                "content": "private upstream output",
            })
            chunk = (
                'data: {"choices":[{"delta":{"content":"answer"},'
                '"finish_reason":"stop"}]}\n\n'
            )
            logger.log_modified_chunk(chunk.encode())
            yield chunk
            logger.discard_buffers()

        @app.post("/v1/chat/completions")
        async def completions() -> StreamingResponse:
            return StreamingResponse(
                successful_stream(),
                media_type="text/event-stream",
            )

        with (
            patch("kiro.debug_logger.debug_logger", logger),
            TestClient(app) as client,
        ):
            response = client.post(
                "/v1/chat/completions",
                headers={"authorization": "Bearer client-secret"},
                json={
                    "model": "claude-opus-5",
                    "messages": [{"role": "user", "content": "private prompt"}],
                },
            )

        assert response.status_code == 200
        captures = list((debug_dir / "requests").iterdir())
        assert len(captures) == 1
        stored = b"\n".join(
            path.read_bytes()
            for path in captures[0].iterdir()
            if path.is_file()
        )
        assert b"private prompt" not in stored
        assert b"client-secret" not in stored
        assert b"upstream-secret" not in stored
        assert b"claude-opus-5" in stored
        replay = json.loads((captures[0] / "replay.json").read_text())
        decoded_upstream = [
            base64.b64decode(item["payload_base64"])
            for item in replay["upstream_chunks"]
        ]
        assert any(
            b"parsed_kiro_event" in item
            for item in decoded_upstream
        )
