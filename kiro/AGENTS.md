# kiro/ — gateway package

Protocol translation between client APIs and the Kiro upstream. 37 modules,
~15.9k lines. Layered: routes -> converters -> http_client -> streaming.

## STRUCTURE

```
kiro/
├── routes_openai.py / routes_anthropic.py / dashboard.py   # HTTP surface
├── converters_core.py                                      # shared payload builder
├── converters_openai.py / converters_anthropic.py          # thin adapters
├── streaming_core.py                                       # KiroEvent + parse_kiro_stream
├── streaming_openai.py / streaming_anthropic.py            # per-protocol serializers
├── parsers.py                                              # AWS event-stream frames
├── sse_validation.py                                       # live order invariants
├── auth.py / account_manager.py / account_errors.py        # credentials + pool
├── http_client.py / network_errors.py / kiro_errors.py     # transport + error mapping
├── debug_logger.py / debug_capture.py / debug_replay.py    # failure forensics
├── payload_guards.py / truncation_*.py                     # request/response repair
└── static/                                                 # frontend build output
```

## WHERE TO LOOK

| Task | File |
|---|---|
| New request field | `models_openai.py` or `models_anthropic.py`, then the matching converter |
| Change history/tool shaping | `converters_core.py` (`build_kiro_payload`) |
| Add an upstream event type | `parsers.py` -> `streaming_core.py` `KiroEvent` -> both serializers |
| Adjust finish/stop mapping | `stop_reasons.py` |
| Token accounting | `tokenizer.py`, `streaming_core.py:calculate_tokens_from_context_usage` |
| Web search emulation | `mcp_tools.py` |

## CONVENTIONS

- Both adapters must consume the same `KiroEvent`. Protocol-specific knowledge
  belongs in `streaming_*.py`, never in `parsers.py`.
- `ModelResolver` never raises; it degrades to pass-through.
- `AccountManager` returns 429 to the caller immediately so failover happens
  before backoff (`docs/router-design.md`).
- Every emitted chunk goes through `sse_validation`; a protocol violation fails
  the stream loudly rather than reaching the client.
- Debug capture redacts content unless `DEBUG_CAPTURE_CONTENT=true`; credential
  patterns stay redacted regardless.

## ANTI-PATTERNS

- Adding a client-specific branch in a serializer. Encode the behavior as a
  request option (`include_reasoning`, `parallel_tool_calls`) instead.
- Duplicating message-shaping logic in `converters_openai.py` or
  `converters_anthropic.py`; extend `converters_core.py`.
- Growing `converters_core.py` or `streaming_anthropic.py` further without
  splitting; both already exceed 1000 lines.
- Editing `static/` — regenerate from `frontend/`.
