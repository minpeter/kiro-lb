import base64
import json
from pathlib import Path
from typing import Any

from kiro.debug_replay import main


def _record(sequence: int, chunk: str) -> dict[str, Any]:
    payload = chunk.encode()
    return {
        "seq": sequence,
        "size": len(payload),
        "stored_size": len(payload),
        "truncated": False,
        "omitted_bytes": 0,
        "payload_base64": base64.b64encode(payload).decode(),
    }


def _event(event_type: str, data: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _write_replay(
    path: Path,
    chunks: list[str],
    capture_content: bool = False,
) -> dict[str, Any]:
    replay = {
        "schema_version": 1,
        "request_id": "request-test",
        "failure": "Invalid assistant content event order",
        "capture_content": capture_content,
        "client_request": {},
        "kiro_request": {},
        "upstream_chunks": [],
        "translated_sse": [
            _record(index, chunk) for index, chunk in enumerate(chunks)
        ],
    }
    path.write_text(json.dumps(replay))
    return replay


class TestDebugReplay:
    def test_replays_invalid_assistant_content_order(
        self,
        tmp_path,
        capsys,
    ):
        replay_path = tmp_path / "replay.json"
        _write_replay(
            replay_path,
            [
                _event("message_start", {"type": "message_start"}),
                _event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text"},
                    },
                ),
                _event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "thinking"},
                    },
                ),
            ],
        )

        exit_code = main(["validate", str(replay_path)])

        assert exit_code == 3
        assert capsys.readouterr().err.strip() == (
            "Invalid assistant content event order"
        )

    def test_valid_capture_exits_cleanly(self, tmp_path):
        replay_path = tmp_path / "replay.json"
        _write_replay(
            replay_path,
            [
                _event("message_start", {"type": "message_start"}),
                _event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text"},
                    },
                ),
                _event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "ok"},
                    },
                ),
                _event(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": 0},
                ),
                _event("message_delta", {"type": "message_delta"}),
                _event("message_stop", {"type": "message_stop"}),
            ],
        )

        assert main(["validate", str(replay_path)]) == 0

    def test_replay_writes_historical_stream_before_failure(
        self,
        tmp_path,
    ):
        replay_path = tmp_path / "replay.json"
        output = tmp_path / "replayed.sse"
        chunks = [
            _event("message_start", {"type": "message_start"}),
            _event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text"},
                },
            ),
            _event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking"},
                },
            ),
        ]
        _write_replay(replay_path, chunks)

        exit_code = main([
            "replay",
            str(replay_path),
            "--protocol",
            "anthropic",
            "--output",
            str(output),
        ])

        assert exit_code == 3
        assert output.read_text() == "".join(chunks)

    def test_export_refuses_content_enabled_capture(
        self,
        tmp_path,
        capsys,
    ):
        replay_path = tmp_path / "replay.json"
        output = tmp_path / "fixture.json"
        _write_replay(replay_path, [], capture_content=True)

        exit_code = main([
            "export-fixture",
            str(replay_path),
            str(output),
        ])

        assert exit_code == 2
        assert "content enabled" in capsys.readouterr().err
        assert not output.exists()
