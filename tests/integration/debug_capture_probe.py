import argparse
import asyncio
import json
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from kiro.debug_logger import debug_logger
from kiro.debug_middleware import DebugLoggerMiddleware
from kiro.debug_replay import load_replay, validate_replay
from kiro.sse_validation import (
    StreamProtocolError,
    begin_anthropic_stream,
    end_anthropic_stream,
)
from kiro.streaming_anthropic import format_sse_event


async def _run_probe(output: Path) -> None:
    started = asyncio.Event()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        started.set()
        yield

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(DebugLoggerMiddleware)
    async def invalid_stream():
        begin_anthropic_stream()
        try:
            debug_logger.log_kiro_request_body(
                b'{"conversationState":{"currentMessage":{"content":"private"}}}'
            )
            debug_logger.log_raw_chunk(b'{"content":"answer"}')
            yield format_sse_event(
                "message_start",
                {"type": "message_start", "message": {"content": []}},
            )
            yield format_sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            )
            yield format_sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "thinking",
                        "thinking": "",
                        "signature": "upstream-signature-secret",
                    },
                },
            )
        finally:
            end_anthropic_stream()

    @app.post("/v1/messages")
    async def messages() -> StreamingResponse:
        return StreamingResponse(invalid_stream(), media_type="text/event-stream")

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", lifespan="on")
    )
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    await asyncio.wait_for(started.wait(), timeout=5)

    http_status = 0
    failure = ""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            async with client.stream(
                "POST",
                f"http://127.0.0.1:{port}/v1/messages",
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
            ) as response:
                http_status = response.status_code
                try:
                    await response.aread()
                except httpx.RemoteProtocolError:
                    failure = "Invalid assistant content event order"
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=5)
        listener.close()

    failures_dir = debug_logger.debug_dir / "failures"
    captures = sorted(
        path
        for path in failures_dir.iterdir()
        if path.is_dir() and not path.name.startswith(".tmp-")
    )
    replay_exit_code = 0
    if captures:
        try:
            validate_replay(load_replay(captures[-1]), "anthropic")
        except StreamProtocolError as exc:
            failure = str(exc)
            replay_exit_code = 3

    output.write_text(json.dumps({
        "http_status": http_status,
        "failure": failure,
        "capture_count": len(captures),
        "replay_exit_code": replay_exit_code,
        "capture_dir": str(captures[-1]) if captures else None,
    }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario",
        choices=("invalid-assistant-content-order",),
        required=True,
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(_run_probe(args.output))


if __name__ == "__main__":
    main()
