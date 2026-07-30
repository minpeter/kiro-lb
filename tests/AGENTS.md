# tests/ — pytest suite

1588 tests, ~41.9k lines. Unit tests dominate; integration tests exercise route
and stream wiring. Full run is under 5 seconds because nothing touches network.

## STRUCTURE

```
tests/
├── conftest.py            # session fixtures incl. network blocking
├── unit/                  # 40 modules, one per kiro/ module
└── integration/           # route/stream flows + manual probes
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

## CONVENTIONS

- `block_all_network_calls` in `conftest.py:419` is session-scoped and autouse.
  Real network access fails the test; mock at the httpx layer.
- Naming is enforced by `pytest.ini`: `test_*.py`, `Test*`, `test_*`.
- Classes group by outcome: `Test*Success`, `Test*Errors`, `Test*EdgeCases`.
- Add tests to the existing module for a subsystem; new files only for new modules.
- Protocol tests assert parsed structure and event order, not prose or exact
  prompt sentences.

## ANTI-PATTERNS

- `time.sleep` or polling for async completion. Await the event/state directly.
- `manual_api_test.py` and `integration/*_probe.py` are live-API scripts, not
  suite members; `norecursedirs` and naming keep them out.
- Asserting only that a value exists. Assert the value.
- Over-mocking a converter so the integration under test cannot fail.
