# tests/ — pytest suite

1725 tests, ~43k lines. Unit tests dominate (45 modules); 4 integration modules
exercise route and stream wiring. Full run is ~6 seconds because nothing touches
network.

## STRUCTURE

```
tests/
├── conftest.py            # 1786 lines: the only conftest, all fixtures + helpers
├── fixtures/              # recorded bad-order payload for validation tests
├── unit/                  # 45 modules, roughly one per kiro/ module
└── integration/           # 4 flow modules + 2 live probes (*_probe.py, not collected)
```

## WHERE TO LOOK

| Target | File |
|---|---|
| OpenAI stream contract | `unit/test_streaming_openai.py` |
| Anthropic block order | `unit/test_streaming_anthropic.py` |
| Cross-protocol invariants | `unit/test_stream_integrity.py` |
| Usage fields each protocol may emit | `unit/test_context_usage_exposure.py` |
| Payload building | `unit/test_converters_core.py` (4933 lines) |
| Payload size guard + trim | `unit/test_payload_guards.py` |
| Encoding/correction per model family | `unit/test_tokenizer.py` |
| Credentials/token lifecycle | `unit/test_auth_manager.py` |
| Frame reassembly, bracket tools | `unit/test_parsers.py` |
| Capture/replay privacy | `unit/test_debug_replay.py`, `integration/test_debug_capture_replay.py` |
| Device login + Builder ID host | `unit/test_device_login.py` |
| Per-key token attribution | `unit/test_key_model_usage.py` |
| Rate chart series | `unit/test_account_rate_series.py` |
| Dashboard views/logs | `unit/test_dashboard_account_view.py`, `unit/test_dashboard_request_logs.py` |

## CONVENTIONS

- `block_all_network_calls` (`conftest.py:416`) is session-scoped and autouse. It
  patches `httpx.AsyncClient` in `kiro.auth`, `kiro.http_client`, and
  `kiro.streaming_openai` (`conftest.py:516`); real network access fails the test.
  Fake streams come back as a real async `aiter_bytes` generator, so code under
  test sees bytes, not a coroutine mock.
- `setup_test_environment` (`conftest.py:45`) is also autouse and repoints
  `ACCOUNTS_CONFIG_FILE`, `ACCOUNTS_STATE_FILE`, and `DASHBOARD_DATA_DIR` at a
  tmp path, patching both `kiro.config` and `main` globals. Never let a test read
  the real `data/`.
- Naming is enforced by `pytest.ini`: `test_*.py`, `Test*`, `test_*`. No custom
  markers are registered; only `asyncio` and `parametrize` are used.
- Classes group by outcome: `Test*Success`, `Test*Errors`, `Test*EdgeCases`.
- Add tests to the existing module for a subsystem; new files only for new
  behavior with no home (`test_context_usage_exposure.py` is the one file added
  since `a9fadd7`).
- Upstream stream bytes come from the `mock_kiro_*_chunks` fixtures or the
  `create_kiro_*_chunk` helpers (`conftest.py:918+`); do not hand-roll a client.
- Protocol tests assert parsed structure and event order, not prose or exact
  prompt sentences.

## ANTI-PATTERNS

- `time.sleep` or polling for async completion. Await the event/state directly.
- `manual_api_test.py` and `integration/*_probe.py` are live-API scripts, not
  suite members; `norecursedirs` and naming keep them out.
- Asserting only that a value exists. Assert the value.
- Over-mocking a converter so the integration under test cannot fail.
