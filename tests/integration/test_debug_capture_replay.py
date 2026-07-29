import json
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

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
