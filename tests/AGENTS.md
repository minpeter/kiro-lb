# tests/ — pytest suite

1694 tests, ~44k lines. Unit tests dominate (44 modules); 4 integration modules
exercise route and stream wiring. Full run is under 10 seconds because nothing
touches network.

## STRUCTURE

```
tests/
├── conftest.py            # session fixtures incl. network blocking
├── fixtures/              # recorded bad-order payloads for validation tests
├── unit/                  # 44 modules, roughly one per kiro/ module
└── integration/           # 4 route/stream flow modules + 2 live probes
```

## WHERE TO LOOK

| Target | File |
|---|---|
| OpenAI stream contract | `unit/test_streaming_openai.py` |
| Anthropic block order | `unit/test_streaming_anthropic.py` |
| Cross-protocol invariants | `unit/test_stream_integrity.py` |
| Payload building | `unit/test_converters_core.py` (5181 lines) |
| Credentials/token lifecycle | `unit/test_auth_manager.py` |
| Frame reassembly, bracket tools | `unit/test_parsers.py` |
| Capture/replay privacy | `unit/test_debug_replay.py`, `integration/test_debug_capture_replay.py` |
| Device login + Builder ID host | `unit/test_device_login.py` |
| Per-key token attribution | `unit/test_key_model_usage.py` |
| Rate chart series | `unit/test_account_rate_series.py` |
| Dashboard views/logs | `unit/test_dashboard_account_view.py`, `unit/test_dashboard_request_logs.py` |

## CONVENTIONS

- `block_all_network_calls` (`conftest.py:420`) is session-scoped and autouse. It
  patches `httpx.AsyncClient` in `kiro.auth`, `kiro.http_client`,
  `kiro.streaming_openai`, and `kiro.account_manager`; real network access fails
  the test. Fake streams come back as a real async `aiter_bytes` generator, so
  code under test sees bytes, not a coroutine mock.
- `setup_test_environment` (`conftest.py:45`) is also autouse and repoints
  `ACCOUNTS_CONFIG_FILE`, `ACCOUNTS_STATE_FILE`, and `DASHBOARD_DATA_DIR` at a
  tmp path. Never let a test read the real `data/`.
- Naming is enforced by `pytest.ini`: `test_*.py`, `Test*`, `test_*`.
- Classes group by outcome: `Test*Success`, `Test*Errors`, `Test*EdgeCases`.
- Add tests to the existing module for a subsystem; new files only for new modules.
- Upstream stream bytes come from the `mock_kiro_*_chunks` fixtures or from
  patching the parser to emit `KiroEvent`s directly; do not hand-roll a client.
- Protocol tests assert parsed structure and event order, not prose or exact
  prompt sentences.

## ANTI-PATTERNS

- `time.sleep` or polling for async completion. Await the event/state directly.
- `manual_api_test.py` and `integration/*_probe.py` are live-API scripts, not
  suite members; `norecursedirs` and naming keep them out.
- Asserting only that a value exists. Assert the value.
- Over-mocking a converter so the integration under test cannot fail.
