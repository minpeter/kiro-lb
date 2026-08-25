# kiro/ — gateway package

Protocol translation between client APIs and the Kiro upstream, plus the
operations control plane. 39 modules, 16.5k lines. Layered:
routes -> converters -> http_client -> streaming.

## STRUCTURE

```
kiro/
├── routes_openai.py / routes_anthropic.py                  # data-plane HTTP surface
├── dashboard.py                                            # control plane: 19 API routes + `/` + SQLite store
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
| Payload size limit | `payload_guards.py`, `config.py` (`KIRO_MAX_PAYLOAD_TOKENS`, legacy `KIRO_MAX_PAYLOAD_BYTES`) |
| Token usage rows (key/account/model) | `usage_tracking.py` -> `dashboard.py` (`flush_key_model_usage`) |
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
- Account selection visits candidates in quota-weighted random order
  (`account_manager.py:_weighted_candidate_order`), weighting by the *fraction*
  of monthly quota left, never the absolute remainder: absolute headroom
  concentrates traffic on the largest plan hard enough to trip its own request
  rate limit. Weighting only reorders — every exclusion (suspension, quota
  quarantine, rate limit, breaker) is still applied per candidate, and every
  account keeps a nonzero chance so none can be starved.
- Routing weight comes from `Account.quota_headroom`, fed by the control-plane
  usage poll (`dashboard.refresh_all_account_usage`) and seeded on load from the
  persisted usage rows (`store.load_quota_headroom`). It is deliberately absent
  from the runtime state document: a stale weight misroutes, and every start or
  blue/green handoff re-seeds instead. `quota_resets_at` and
  `quota_overage_enabled` follow the same rule and are seeded together by
  `store.load_quota_period`.
- A 402 `MONTHLY_REQUEST_COUNT` quarantine runs to the reported quota reset
  (`_quota_quarantine_until`), not for a fixed interval: `ACCOUNT_QUOTA_QUARANTINE`
  is only the floor and the fallback when no reset date is known, with
  `ACCOUNT_QUOTA_QUARANTINE_MAX` capping a stale date. A fixed 6h window was
  measured releasing accounts ~26 days early, back into the pool at 1000/1000.
- `quota_depleted` excludes an account (`is_quota_depleted`): usage reports the
  allowance spent with overage off, so it cannot serve. It is the same condition
  as `quota_exhausted` reached by weaker evidence — telemetry rather than an
  upstream 402 — so it is the one exclusion that can be wrong, and
  `get_next_account` therefore runs a **second last-resort pass** that lifts only
  this exclusion. A stalled usage poll must never be able to empty the pool.
  Every other exclusion is backed by an upstream response and always applies.
- The two quota states are worded in parallel on purpose ("monthly quota
  exhausted" / "monthly quota spent", both `excluded for ...`) in
  `describe_pool_state` and the dashboard badge. They are the same operational
  fact; do not make one read milder than the other.
- A refresh token the auth host rejects raises `CredentialDeadError`
  (`account_errors.py`), never a bare `httpx.HTTPStatusError`. That exception is
  neither `RequestError` nor `TimeoutException`, so it matched no handler in
  `request_with_retry` and none in the routes' `except HTTPException` — it escaped
  as a 500 with no `report_failure`, leaving a permanently dead account in
  rotation. `get_access_token` and `force_refresh` translate it at the single
  choke point, **after** the raw-source reload and the SQLite graceful-degradation
  fallback have had their chance; only 400/401 convert, so a 5xx from the auth
  host keeps its transient retry meaning.
- `auth_dead` (`Account.auth_dead_until`) ranks **above** `suspended` in
  `account_routing_state`: a suspension is a reachable upstream verdict, whereas an
  account that cannot obtain a token has nothing left to ask. Like a suspension it
  leaves the Circuit Breaker untouched — a probabilistic retry would spend real
  requests re-proving a dead credential. `report_success` clears it, because a
  served request outranks any stored prediction of death. Persisted (the condition
  outlives a restart), and its pool-state text names the remedy ("re-login
  required") rather than the symptom, since unlike a suspension this one is the
  operator's to fix.
- A stored usage-poll error is operator-facing text in a table cell, so
  `_summarize_usage_error` (`dashboard.py`) bounds it and strips newlines and the
  endpoint URL. Persisting `str(exc)` put httpx's 188-character two-line message in
  the accounts table and pushed every later column off-screen. Do not widen it back
  to a raw upstream string.
- `request_rate_series` reports each series' `routingState` alongside its buckets.
  The dashboard hides accounts that cannot serve the next request from the rate
  chart, and it must not have to join the accounts endpoint (pool as of now)
  against a windowed history to decide. Series are seeded from the live pool, so a
  deregistered account drops out entirely rather than charting with a null state.
- `_current_account_index` is no longer the selection cursor. It records the last
  success and is the rotation start only for the legacy sticky policy
  (`ACCOUNT_QUOTA_WEIGHTED_ROUTING=false`, the rollback switch).
- Every emitted chunk goes through `sse_validation`; a protocol violation fails
  the stream loudly rather than reaching the client.
- Debug capture redacts content unless `DEBUG_CAPTURE_CONTENT=true`; credential
  patterns stay redacted regardless.
- Accounting and dashboard store writes never break the data plane: they catch
  their own exceptions and return a neutral value (`usage_tracking.py:42`,
  `dashboard.py:265`).
- The calling key's identity travels in a `ContextVar`, not through converter and
  serializer signatures (`usage_tracking.py`). The serving account travels the same
  way (`current_account_id`), set once per **attempt** in both route modules so a
  failover attributes tokens to the account that actually answered rather than the
  first one tried. A module-level global would cross-attribute concurrent requests.
- `flush_key_model_usage` writes `key_model_usage` and `account_model_usage` from
  one drained batch in a single transaction. The per-key table is the sum of the
  per-account one over accounts, and that invariant only holds if neither can be
  written without the other.
- Tokens with no account are bucketed as `UNKNOWN_ACCOUNT_ID` ("unknown"), never
  dropped: legacy single-account mode records without going through selection, and
  discarding those rows would make the two views silently disagree. That id is not
  a credential path, so it must not be passed through `account_label`.
- `account_model_usage` history cannot be backfilled. `request_logs` carries no
  account id, so rows predating this table stay unattributed by design; the panel
  says so rather than showing zero.
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
- Persisting Kiro CLI's Builder ID fallback profile as the account's own
  profile. Generation and management requests use it only as a request-scoped
  service routing value; the credential remains profile-less.
- Adding a field to a protocol's usage object that the protocol does not define.
  `credits_used` on OpenAI is the one sanctioned extension.
- Counting tokens with a hardcoded encoding or a blanket correction factor. Go
  through `resolve_token_profile`; the correction applies to CJK only, scaled by
  the measured ratio (`tokenizer.py:129`).
- Serializing a payload for measurement differently from how the routes send it.
  `check_payload_size` uses `ensure_ascii=False` deliberately
  (`payload_guards.py:52`).
- Storing anything sensitive in the dashboard store; it holds metadata only.
