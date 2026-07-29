import asyncio
import json
import socket
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator, cast

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from kiro.streaming_anthropic import stream_kiro_to_anthropic
from kiro.streaming_openai import stream_kiro_to_openai_internal
from kiro.sse_validation import AnthropicSSEValidator

if TYPE_CHECKING:
    from kiro.auth import KiroAuthManager
    from kiro.cache import ModelInfoCache


class FakeResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.closed = False

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class ModelCache:
    def get_max_input_tokens(self, model: str) -> int:
        return 200000


def _unicode_chunks() -> list[bytes]:
    return [
        bytes.fromhex("7b22636f6e74656e74223a22e4"),
        bytes.fromhex("bda0e5a5"),
        bytes.fromhex("bdf09f"),
        bytes.fromhex("8c8d227d"),
        b'{"stopReason":"END_TURN"}',
        b'{"contextUsagePercentage":1}',
    ]


def _tool_chunks() -> list[bytes]:
    return [
        b'{"name":"lookup","toolUseId":"toolu_A"}',
        b'{"input":"{\\"q\\":\\"same\\"}"}',
        b'{"stop":true}',
        b'{"name":"lookup","toolUseId":"toolu_B"}',
        b'{"input":"{\\"q\\":\\"same\\"}"}',
        b'{"stop":true}',
        b'{"stopReason":"TOOL_USE"}',
        b'{"contextUsagePercentage":1}',
    ]


def _malformed_chunks() -> list[bytes]:
    malformed = '{"path":"/tmp/x","content":"unterminated'
    return [
        b'{"name":"write_file","toolUseId":"toolu_cut"}',
        json.dumps({"input": malformed}).encode(),
        b'{"stop":true}',
    ]


def _chronology_chunks() -> list[bytes]:
    return [
        b'{"text":"T1"}',
        b'{"content":"A1"}',
        b'{"signature":"S1"}',
        b'{"name":"lookup","toolUseId":"toolu_A"}',
        b'{"input":"{\\"q\\":\\"same\\"}"}',
        b'{"stop":true}',
        b'{"name":"lookup","toolUseId":"toolu_B"}',
        b'{"input":"{\\"q\\":\\"same\\"}"}',
        b'{"stop":true}',
        b'{"text":"T2"}',
        b'{"content":"A2"}',
        b'{"signature":"S2"}',
        b'{"stopReason":"TOOL_USE"}',
        b'{"contextUsagePercentage":1}',
    ]


async def _run() -> dict[str, bool]:
    started = asyncio.Event()
    cache = ModelCache()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        started.set()
        yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/anthropic/{scenario}")
    async def anthropic(scenario: str) -> StreamingResponse:
        response = FakeResponse(_scenario_chunks(scenario))

        async def stream() -> AsyncIterator[str]:
            async for chunk in stream_kiro_to_anthropic(
                cast(httpx.Response, cast(object, response)),
                "claude-opus-5",
                cast("ModelInfoCache", cast(object, cache)),
                cast("KiroAuthManager", object()),
                request_messages=[{"role": "user", "content": "probe"}],
            ):
                yield chunk

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/openai/{scenario}")
    async def openai(scenario: str) -> StreamingResponse:
        response = FakeResponse(_scenario_chunks(scenario))
        client = httpx.AsyncClient()

        async def stream() -> AsyncIterator[str]:
            try:
                async for chunk in stream_kiro_to_openai_internal(
                    client,
                    cast(httpx.Response, cast(object, response)),
                    "claude-opus-5",
                    cast("ModelInfoCache", cast(object, cache)),
                    cast("KiroAuthManager", object()),
                    request_messages=[{"role": "user", "content": "probe"}],
                ):
                    yield chunk
            finally:
                await client.aclose()

        return StreamingResponse(stream(), media_type="text/event-stream")

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", lifespan="on")
    )
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    await asyncio.wait_for(started.wait(), timeout=5)

    results: dict[str, object] = {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for protocol in ("anthropic", "openai"):
                for scenario in ("unicode", "tools"):
                    response = await client.get(
                        f"http://127.0.0.1:{port}/{protocol}/{scenario}"
                    )
                    results[f"{protocol}_{scenario}"] = {
                        "status": response.status_code,
                        "body": response.text,
                    }
                malformed_clean = False
                try:
                    async with client.stream(
                        "GET",
                        f"http://127.0.0.1:{port}/{protocol}/malformed",
                    ) as response:
                        body = await response.aread()
                        malformed_clean = _is_clean_terminal(protocol, body)
                except httpx.RemoteProtocolError:
                    malformed_clean = False
                results[f"{protocol}_malformed_clean"] = malformed_clean
            chronology = await client.get(
                f"http://127.0.0.1:{port}/anthropic/chronology"
            )
            results["anthropic_chronology"] = {
                "status": chronology.status_code,
                "body": chronology.text,
            }
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=5)
        listener.close()

    return _summarize(results)


def _scenario_chunks(scenario: str) -> list[bytes]:
    if scenario == "unicode":
        return _unicode_chunks()
    if scenario == "tools":
        return _tool_chunks()
    if scenario == "malformed":
        return _malformed_chunks()
    if scenario == "chronology":
        return _chronology_chunks()
    raise ValueError(scenario)


def _is_clean_terminal(protocol: str, body: bytes) -> bool:
    if protocol == "anthropic":
        return b'"type": "message_stop"' in body
    return b"data: [DONE]" in body


def _summarize(results: dict[str, object]) -> dict[str, bool]:
    anthropic_unicode = results["anthropic_unicode"]
    openai_unicode = results["openai_unicode"]
    anthropic_tools = results["anthropic_tools"]
    openai_tools = results["openai_tools"]
    assert isinstance(anthropic_unicode, dict)
    assert isinstance(openai_unicode, dict)
    assert isinstance(anthropic_tools, dict)
    assert isinstance(openai_tools, dict)
    chronology = results["anthropic_chronology"]
    assert isinstance(chronology, dict)
    chronology_valid, chronology_tools, chronology_signatures = (
        _validate_chronology(str(chronology["body"]))
    )
    summary = {
        "anthropic_unicode": "你好🌍" in str(anthropic_unicode["body"]),
        "openai_unicode": "你好🌍" in str(openai_unicode["body"]),
        "anthropic_tool_ids": all(
            tool_id in str(anthropic_tools["body"])
            for tool_id in ("toolu_A", "toolu_B")
        ),
        "openai_tool_ids": all(
            tool_id in str(openai_tools["body"])
            for tool_id in ("toolu_A", "toolu_B")
        ),
        "anthropic_malformed_clean": bool(
            results["anthropic_malformed_clean"]
        ),
        "openai_malformed_clean": bool(results["openai_malformed_clean"]),
        "anthropic_chronology": chronology_valid,
        "anthropic_chronology_tools": chronology_tools,
        "anthropic_chronology_signatures": chronology_signatures,
    }
    assert all([
        summary["anthropic_unicode"],
        summary["openai_unicode"],
        summary["anthropic_tool_ids"],
        summary["openai_tool_ids"],
        not summary["anthropic_malformed_clean"],
        not summary["openai_malformed_clean"],
        summary["anthropic_chronology"],
        summary["anthropic_chronology_tools"],
        summary["anthropic_chronology_signatures"],
    ])
    return summary


def _validate_chronology(body: str) -> tuple[bool, bool, bool]:
    validator = AnthropicSSEValidator()
    starts = []
    signatures = []
    for frame in body.split("\n\n"):
        event_type = None
        data = None
        for line in frame.splitlines():
            if line.startswith("event:"):
                event_type = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data = json.loads(line.removeprefix("data:").strip())
        if event_type is None or data is None:
            continue
        validator.accept(event_type, data)
        if event_type == "content_block_start":
            starts.append(data["content_block"])
        if (data.get("delta") or {}).get("type") == "signature_delta":
            signatures.append((data["index"], data["delta"]["signature"]))
    validator.finish()
    types = [block["type"] for block in starts]
    tool_ids = [
        block["id"] for block in starts if block["type"] == "tool_use"
    ]
    return (
        types == [
            "thinking",
            "text",
            "tool_use",
            "tool_use",
            "thinking",
            "text",
        ],
        tool_ids == ["toolu_A", "toolu_B"],
        signatures == [(0, "S1"), (4, "S2")],
    )


if __name__ == "__main__":
    print(json.dumps(asyncio.run(_run()), ensure_ascii=False, indent=2))
