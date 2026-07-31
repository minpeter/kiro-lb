# kiro/ — gateway package

Protocol translation between client APIs and the Kiro upstream, plus the
operations control plane. 39 modules, 16.5k lines. Layered:
routes -> converters -> http_client -> streaming.

## STRUCTURE

```
kiro/
├── routes_openai.py / routes_anthropic.py                  # data-plane HTTP surface
├── dashboard.py                                            # control plane: 17 API routes + `/` + SQLite store
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
├── tokenizer.py                                            # per-family encoding + CJK correction
├── debug_logger.py / debug_middleware.py                   # request logging
├── debug_capture.py / debug_replay.py / debug_sanitize.py  # failure forensics + redaction
├── payload_guards.py                                       # size guard + history trim
└── static/                                                 # frontend build output
```

No module has been added or removed since `a9fadd7`; 37 of the 39 were modified.

## WHERE TO LOOK

| Task | File |
|---|---|
| New request field | `models_openai.py` or `models_anthropic.py`, then the matching converter |
| Change history/tool shaping | `converters_core.py` (`build_kiro_payload`) |
| Add an upstream event type | `parsers.py` -> `streaming_core.py` `KiroEvent` -> both serializers |
| Adjust finish/stop mapping | `stop_reasons.py` |
| Token counting | `tokenizer.py` (`resolve_token_profile`), `streaming_core.py:calculate_tokens_from_context_usage` |
| Model list + per-model limits | `routes_openai.py:203` (`_resolve_model_limits` at `:180`) |
| Payload size limit | `payload_guards.py`, `config.py:482` (`KIRO_MAX_PAYLOAD_BYTES`) |
| Per-key/model usage rows | `usage_tracking.py` -> `dashboard.py:206` (`flush_key_model_usage`) |
| Web search emulation | `mcp_tools.py` (transport shim, not a tool framework) |
| Extended thinking budget rules | `native_thinking.py` (min 1024; unknown fields rejected) |
| Rate estimate + chart data | `account_manager.py:1047` (`estimate_rate_limit`), `:1103` (`request_rate_series`) |
| New login provider | `device_login.py`, then `dashboard.py` device-login routes |

## CONVENTIONS

- Both adapters must consume the same `KiroEvent`. Protocol-specific knowledge
  belongs in `streaming_*.py`, never in `parsers.py`.
- `ModelResolver` never raises; it degrades to pass-through. `resolve_token_profile`
  is the same contract for token counting: an unknown name falls back to the
  Claude profile, which over-counts rather than under-counts (`tokenizer.py:87`).
- `AccountManager` returns 429 to the caller immediately so failover happens
  before backoff.
- Account selection always starts from the global index
  (`account_manager.py:786`) and that index moves only on success
  (`account_manager.py:999`).
- Every emitted chunk goes through `sse_validation`; a protocol violation fails
  the stream loudly rather than reaching the client.
- Debug capture redacts content unless `DEBUG_CAPTURE_CONTENT=true`; credential
  patterns stay redacted regardless.
- Accounting and dashboard store writes never break the data plane: they catch
  their own exceptions and return a neutral value (`usage_tracking.py:42`,
  `dashboard.py:265`).
- The calling key's identity travels in a `ContextVar`, not through converter and
  serializer signatures (`usage_tracking.py`).
- Social and Builder ID login polling are deliberately not shared; their pending
  semantics are near-inverses (HTTP 200 + `status` vs HTTP 400 + `authorization_pending`).
- `/v1/messages/count_tokens` (`routes_anthropic.py:740`) must use the same
  estimator as the Anthropic `message_start` event: it runs before the request, so
  no upstream context usage exists yet.

## ANTI-PATTERNS

- Adding a client-specific branch in a serializer. Encode the behavior as a
  request option (`include_reasoning`, `parallel_tool_calls`) instead.
- Duplicating message-shaping logic in `converters_openai.py` or
  `converters_anthropic.py`; extend `converters_core.py`.
- Growing `converters_core.py` (1363), `account_manager.py` (1252) or
  `streaming_anthropic.py` (979) further without splitting.
- Editing `static/` — regenerate from `frontend/`.
- Recreating a dashboard SQLite table to add a column. Follow the additive
  `PRAGMA table_info` + `ALTER TABLE` pattern (`dashboard.py:96`, `:147`).
- Exposing the device code or token in a device-flow response; `DeviceFlow.view`
  is the only client-facing shape (`device_login.py:82`). Registration discards
  the tokens either way (`dashboard.py:654`).
- Giving a Builder ID account the global fallback profile ARN
  (`routes_openai.py:371`, `routes_anthropic.py:255`).
- Adding a field to a protocol's usage object that the protocol does not define.
  `credits_used` on OpenAI is the one sanctioned extension.
- Counting tokens with a hardcoded encoding or a blanket correction factor. Go
  through `resolve_token_profile`; the correction applies to CJK only, scaled by
  the measured ratio (`tokenizer.py:129`).
- Serializing a payload for measurement differently from how the routes send it.
  `check_payload_size` uses `ensure_ascii=False` deliberately
  (`payload_guards.py:52`).
- Storing anything sensitive in the dashboard store; it holds metadata only.
