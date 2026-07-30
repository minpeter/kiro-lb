# kiro/ — gateway package

Protocol translation between client APIs and the Kiro upstream, plus the
operations control plane. 39 modules, ~16.5k lines. Layered:
routes -> converters -> http_client -> streaming.

## STRUCTURE

```
kiro/
├── routes_openai.py / routes_anthropic.py                  # data-plane HTTP surface
├── dashboard.py                                            # control plane: 17 API routes + SQLite store
├── device_login.py / accounts_admin.py                     # add an account without shell access
├── usage_tracking.py / usage.py                            # per-key tokens; upstream quota lookup
├── converters_core.py                                      # shared payload builder
├── converters_openai.py / converters_anthropic.py          # thin adapters
├── models_openai.py / models_anthropic.py                  # pydantic request/response schemas
├── native_thinking.py                                      # extended-thinking budget validation
├── mcp_tools.py                                            # web_search bridge + SSE emulation
├── streaming_core.py                                       # KiroEvent + parse_kiro_stream
├── streaming_openai.py / streaming_anthropic.py            # per-protocol serializers
├── parsers.py                                              # AWS event-stream frames
├── sse_validation.py                                       # live order invariants
├── auth.py / account_manager.py / account_errors.py        # credentials + pool
├── http_client.py / network_errors.py / kiro_errors.py     # transport + error mapping
├── debug_logger.py / debug_middleware.py                   # request logging
├── debug_capture.py / debug_replay.py / debug_sanitize.py  # failure forensics + redaction
├── payload_guards.py                                       # oversize payload repair
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
| Per-key/model usage rows | `usage_tracking.py` -> `dashboard.py:flush_key_model_usage` |
| Web search emulation | `mcp_tools.py` (transport shim, not a tool framework) |
| Extended thinking budget rules | `native_thinking.py` (min 1024; unknown fields rejected) |
| Request/response schema change | `models_openai.py` / `models_anthropic.py` |
| Rate estimate + chart data | `account_manager.py:estimate_rate_limit`, `:request_rate_series` |
| New login provider | `device_login.py`, then `dashboard.py` device-login routes |

## CONVENTIONS

- Both adapters must consume the same `KiroEvent`. Protocol-specific knowledge
  belongs in `streaming_*.py`, never in `parsers.py`.
- `ModelResolver` never raises; it degrades to pass-through.
- `AccountManager` returns 429 to the caller immediately so failover happens
  before backoff.
- Account selection always starts from the global index
  (`account_manager.py:791`) and that index moves only on success
  (`account_manager.py:1009`).
- Every emitted chunk goes through `sse_validation`; a protocol violation fails
  the stream loudly rather than reaching the client.
- Debug capture redacts content unless `DEBUG_CAPTURE_CONTENT=true`; credential
  patterns stay redacted regardless.
- Accounting and dashboard store writes never break the data plane: they catch
  their own exceptions and return a neutral value (`usage_tracking.py:41`,
  `dashboard.py:261`).
- The calling key's identity travels in a `ContextVar`, not through converter and
  serializer signatures (`usage_tracking.py`).
- Social and Builder ID login polling are deliberately not shared; their pending
  semantics are near-inverses (HTTP 200 + `status` vs HTTP 400 + `authorization_pending`).

## ANTI-PATTERNS

- Adding a client-specific branch in a serializer. Encode the behavior as a
  request option (`include_reasoning`, `parallel_tool_calls`) instead.
- Duplicating message-shaping logic in `converters_openai.py` or
  `converters_anthropic.py`; extend `converters_core.py`.
- Growing `converters_core.py`, `streaming_anthropic.py`, or `account_manager.py`
  further without splitting; all three already exceed 1000 lines.
- Editing `static/` — regenerate from `frontend/`.
- Recreating a dashboard SQLite table to add a column. Follow the additive
  `PRAGMA table_info` + `ALTER TABLE` pattern in `dashboard.py:148`.
- Exposing the device code or token in a device-flow response; `DeviceFlow.view`
  is the only client-facing shape (`device_login.py:83`).
- Giving a Builder ID account the global fallback profile ARN
  (`routes_openai.py:268`, `routes_anthropic.py:273`).
- Storing anything sensitive in the dashboard store; it holds metadata only.
