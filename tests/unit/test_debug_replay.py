import base64
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro.debug_replay import main
from kiro.debug_sanitize import sanitize_bytes, sanitize_value
from kiro.sse_validation import (
    AnthropicSSEValidator,
    OpenAIStreamValidator,
    StreamProtocolError,
    validate_anthropic_records,
)
from kiro.streaming_anthropic import stream_kiro_to_anthropic
from kiro.streaming_core import KiroEvent


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
        "translated_sse": [_record(index, chunk) for index, chunk in enumerate(chunks)],
    }
    path.write_text(json.dumps(replay))
    return replay


class TestDebugReplay:
    def test_anthropic_rejects_content_after_message_delta(self):
        validator = AnthropicSSEValidator()
        validator.accept("message_start", {"type": "message_start"})
        validator.accept(
            "content_block_start",
            {
                "index": 0,
                "content_block": {"type": "text"},
            },
        )
        validator.accept("content_block_stop", {"index": 0})
        validator.accept("message_delta", {"type": "message_delta"})

        with pytest.raises(StreamProtocolError):
            validator.accept(
                "content_block_start",
                {
                    "index": 1,
                    "content_block": {"type": "text"},
                },
            )

    def test_openai_rejects_content_after_finish_reason(self):
        validator = OpenAIStreamValidator()
        validator.accept(
            {
                "choices": [
                    {
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ]
            }
        )

        with pytest.raises(StreamProtocolError):
            validator.accept(
                {
                    "choices": [
                        {
                            "delta": {"content": "late"},
                            "finish_reason": None,
                        }
                    ]
                }
            )

    @pytest.mark.asyncio
    async def test_fixed_translator_replays_capture_without_order_failure(
        self,
    ):
        fixture_path = Path(__file__).parents[1] / "fixtures" / "invalid_assistant_content_event_order.json"
        fixture = json.loads(fixture_path.read_text())

        async def raw_events(*args, **kwargs):
            for raw_event in fixture["raw_events"]:
                yield KiroEvent(**raw_event)
            yield KiroEvent(type="context_usage", context_usage_percentage=5.0)

        response = AsyncMock()
        response.aclose = AsyncMock()
        model_cache = MagicMock()
        model_cache.get_max_input_tokens.return_value = 200000
        chunks = []
        with patch(
            "kiro.streaming_anthropic.parse_kiro_stream",
            raw_events,
        ):
            async for chunk in stream_kiro_to_anthropic(
                response,
                "claude-opus-5",
                model_cache,
                MagicMock(),
            ):
                chunks.append(chunk)

        records = [_record(index, chunk) for index, chunk in enumerate(chunks)]
        validate_anthropic_records(records)

    def test_historical_fixture_replays_invalid_order(self, capsys):
        fixture = Path(__file__).parents[1] / "fixtures" / "invalid_assistant_content_event_order.json"

        exit_code = main(["validate", str(fixture)])

        assert exit_code == 3
        assert capsys.readouterr().err.strip() == ("Invalid assistant content event order")

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
        assert capsys.readouterr().err.strip() == ("Invalid assistant content event order")

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

        exit_code = main(
            [
                "replay",
                str(replay_path),
                "--protocol",
                "anthropic",
                "--output",
                str(output),
            ]
        )

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

        exit_code = main(
            [
                "export-fixture",
                str(replay_path),
                str(output),
            ]
        )

        assert exit_code == 2
        assert "content enabled" in capsys.readouterr().err
        assert not output.exists()

    def test_export_refuses_base64_encoded_secret(
        self,
        tmp_path,
        capsys,
    ):
        replay_path = tmp_path / "replay.json"
        output = tmp_path / "fixture.json"
        _write_replay(
            replay_path,
            ["Authorization: Bearer encoded-secret-token\n"],
        )

        exit_code = main(
            [
                "export-fixture",
                str(replay_path),
                str(output),
            ]
        )

        assert exit_code == 2
        assert "credential pattern" in capsys.readouterr().err
        assert not output.exists()

    def test_sanitizer_handles_prefixes_key_suffixes_and_encoded_json(self):
        prefixed = sanitize_bytes(
            b'private-prefix {"content":"hello"}',
            capture_content=False,
        ).decode()
        assert "private-prefix" not in prefixed
        assert '"chars":15' in prefixed

        sanitized = sanitize_value(
            {
                "providerToken": "provider-value",
                "awsAccessKeyId": "aws-value",
                "browserSession": "session-value",
                "customSignature": "signature-value",
                "arguments": json.dumps(
                    {
                        "nestedProviderToken": "nested-value",
                        "message": "visible",
                    }
                ),
            },
            capture_content=True,
        )
        assert sanitized["providerToken"] == "[REDACTED]"
        assert sanitized["awsAccessKeyId"] == "[REDACTED]"
        assert sanitized["browserSession"] == "[REDACTED]"
        assert sanitized["customSignature"] == "[REDACTED_SIGNATURE]"
        encoded = json.loads(sanitized["arguments"])
        assert encoded["nestedProviderToken"] == "[REDACTED]"
        assert encoded["message"] == "visible"

    @pytest.mark.parametrize("payload", ["not base64!", None])
    def test_export_fails_closed_on_invalid_base64(
        self,
        tmp_path,
        capsys,
        payload,
    ):
        replay_path = tmp_path / "replay.json"
        output = tmp_path / "fixture.json"
        replay = _write_replay(replay_path, ["safe"])
        replay["translated_sse"][0]["payload_base64"] = payload
        replay_path.write_text(json.dumps(replay))

        assert main(["export-fixture", str(replay_path), str(output)]) == 2
        assert "invalid base64" in capsys.readouterr().err
        assert not output.exists()

    @pytest.mark.parametrize("in_payload", [False, True])
    def test_export_refuses_unredacted_sensitive_fields(
        self,
        tmp_path,
        capsys,
        in_payload,
    ):
        replay_path = tmp_path / "replay.json"
        output = tmp_path / "fixture.json"
        if in_payload:
            _write_replay(
                replay_path,
                ['data: {"providerToken":"opaque-value"}\n'],
            )
        else:
            replay = _write_replay(replay_path, [])
            replay["client_request"] = {"aws_session_token": "opaque-value"}
            replay_path.write_text(json.dumps(replay))

        assert main(["export-fixture", str(replay_path), str(output)]) == 2
        assert "credential pattern" in capsys.readouterr().err
        assert not output.exists()

    def test_export_uses_private_atomic_temporary_file(self, tmp_path):
        replay_path = tmp_path / "replay.json"
        output = tmp_path / "fixture.json"
        _write_replay(replay_path, [])
        real_replace = __import__("os").replace
        observed = []

        def inspect_replace(source, destination):
            source = Path(source)
            observed.append((source, Path(destination)))
            assert source != output
            assert source.stat().st_mode & 0o777 == 0o600
            real_replace(source, destination)

        with patch("kiro.debug_replay.os.replace", inspect_replace):
            assert main(["export-fixture", str(replay_path), str(output)]) == 0

        assert observed and observed[0][1] == output
        assert output.stat().st_mode & 0o777 == 0o600

    def test_replay_output_permissions_are_private(self, tmp_path):
        replay_path = tmp_path / "replay.json"
        output = tmp_path / "replayed.sse"
        _write_replay(
            replay_path,
            [
                _event("message_start", {"type": "message_start"}),
                _event(
                    "message_delta",
                    {"type": "message_delta"},
                ),
                _event("message_stop", {"type": "message_stop"}),
            ],
        )

        assert (
            main(
                [
                    "replay",
                    str(replay_path),
                    "--protocol",
                    "anthropic",
                    "--output",
                    str(output),
                ]
            )
            == 0
        )
        assert output.stat().st_mode & 0o777 == 0o600
