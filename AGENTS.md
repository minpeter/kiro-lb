# PROJECT KNOWLEDGE BASE

**Commit:** a9fadd7
**Branch:** main

## OVERVIEW

`kiro-lb` is a FastAPI reverse-engineered proxy that exposes Kiro (Amazon Q
Developer / CodeWhisperer) through OpenAI- and Anthropic-compatible APIs, load
balances across a pool of Kiro accounts, and ships a private React operations
dashboard. Python 3.10 (`Dockerfile`, CI), httpx, loguru, tiktoken. AGPL-3.0,
partly derived from Kiro Gateway (`jwadow/kiro-gateway`) — keep the per-file
license headers.

## STRUCTURE

```
kiro-lb-python/
├── main.py                  # App factory, lifespan, CLI, static mounts (855 lines)
├── kiro/                    # Gateway package: 39 modules, ~16.5k lines
│   └── static/              # BUILD OUTPUT of frontend/ — never hand-edit
├── frontend/                # Bun + Vite + React 19 dashboard source
├── tests/                   # pytest, 1694 tests; network-blocked by conftest
├── data/                    # Runtime credentials/state/sqlite (gitignored)
├── debug_logs/              # Capture output when DEBUG_MODE is on (gitignored)
├── docker-compose.yml       # Upstream-style deployment (service kiro-gateway)
├── docker-compose.homelab.yml  # This operator's live deployment (bound to 10.10.10.10)
└── manual_api_test.py       # Manual live-API script, excluded from pytest
```

No `docs/`, root `README.md`, or license/community docs exist any more: `48e728d`
removed the root docs, `a9fadd7` removed the whole `docs/` tree (ADR 0001,
router design, ARCHITECTURE, 8 translated READMEs). The invariants they held are
folded into this file; do not link to them.

## WHERE TO LOOK

| Task | Location | Notes |
|---|---|---|
| Add/modify a client endpoint | `kiro/routes_openai.py`, `kiro/routes_anthropic.py` | Only 4 + 2 public routes exist |
| Request -> Kiro payload | `kiro/converters_core.py` | 1379 lines; both adapters delegate here |
| Kiro -> client stream | `kiro/streaming_openai.py`, `kiro/streaming_anthropic.py` | Shared event model in `kiro/streaming_core.py` |
| AWS event-stream framing | `kiro/parsers.py` | Frame reassembly + bracket tool-call recovery |
| Stream order invariants | `kiro/sse_validation.py` | Raises mid-stream instead of shipping bad order |
| Account failover / rotation | `kiro/account_manager.py` | Circuit breaker, global sticky, lazy init |
| Credentials + host selection | `kiro/auth.py`, `kiro/config.py` | 4 sources; Builder ID routes to a different host |
| Add an account by browser login | `kiro/device_login.py` | Social + Builder ID device flows |
| Dashboard API / SQLite store | `kiro/dashboard.py` | 17 routes under `/api/dashboard`, plus `/` |
| Per-key token accounting | `kiro/usage_tracking.py` | ContextVar identity, batched flush |
| Model name handling | `kiro/model_resolver.py` | Never raises; unknown names pass through |
| Request/response schemas | `kiro/models_openai.py`, `kiro/models_anthropic.py` | Extra-open models preserve unknown fields |
| Extended thinking budgets | `kiro/native_thinking.py` | Budget must be >= 1024; unknown fields rejected upstream |
| Failure capture/replay | `kiro/debug_capture.py`, `kiro/debug_replay.py`, `kiro/debug_sanitize.py` | Redacted by default |
| Payload size repair | `kiro/payload_guards.py` | Measured cutoff: 1,085,435 bytes pass, 1,086,459 fail (`config.py:456`) |

## CODE MAP

| Symbol | Type | Location | Role |
|---|---|---|---|
| `build_kiro_payload` | function | `kiro/converters_core.py:1206` | Single funnel for every upstream request |
| `parse_kiro_stream` | async gen | `kiro/streaming_core.py:112` | Only place raw frames become `KiroEvent` |
| `KiroEvent` | dataclass | `kiro/streaming_core.py:48` | Protocol-neutral event both adapters consume |
| `AccountManager` | class | `kiro/account_manager.py:258` | Pool state, failover, rate series, persistence |
| `AccountManager.get_next_account` | method | `kiro/account_manager.py:731` | Rotation from the global index; skips quota-exhausted accounts |
| `AccountManager.report_failure` | method | `kiro/account_manager.py:918` | Classifies 429 / 402 / INVALID_MODEL_ID separately |
| `KiroAuthManager` | class | `kiro/auth.py:67` | Token lifecycle, region + host resolution |
| `ModelResolver` | class | `kiro/model_resolver.py:241` | normalize -> cache -> hidden -> passthrough |
| `validate_live_openai_payload` | function | `kiro/sse_validation.py:204` | Fails the stream instead of shipping bad order |
| `identify_data_api_key` | function | `kiro/dashboard.py:288` | Legacy env key -> `ROOT_KEY_ID`, else hashed `klb_` key |
| `record_token_usage` | function | `kiro/usage_tracking.py:30` | Attributes tokens to the calling key |
| `start_device_login` | function | `kiro/device_login.py:299` | Social vs Builder ID flows, deliberately unshared |
| `get_kiro_api_host` / `get_kiro_q_host` | function | `kiro/config.py:578`, `:584` | Builder ID flag picks the host template |

Line numbers drift on every edit to these files. Grep the symbol name before
trusting a pin here.

## CONVENTIONS

- Reasoning is forwarded only from native upstream frames. Never synthesize it
  from response text or prompt tags.
- OpenAI reasoning is emitted as `reasoning` (`streaming_openai.py:187`), not the
  legacy `reasoning_content`; requests accept both on input.
- Streaming uses a per-request `httpx.AsyncClient`; non-streaming uses the shared
  pooled client from `lifespan`. Reusing the shared client for streams leaks CLOSE_WAIT.
- Any new client-visible behavior must land on OpenAI **and** Anthropic, in both
  streaming and non-streaming paths.
- Control and data planes stay separate: dashboard cookie sessions cannot call
  `/v1`, and `/v1` API keys cannot call `/api/dashboard`.
- Dashboard SQLite stores metadata only — no prompts, completions, raw keys, or
  refresh tokens. New columns arrive via additive `PRAGMA table_info` +
  `ALTER TABLE` migration (`dashboard.py:148`), never a destructive rewrite.
- Proxy env vars are set before any httpx client exists (`main.py:180`); creating
  a client earlier silently ignores the VPN/proxy config.
- `pytest.ini`, `kiro/__init__.py`, and some legacy comments are Russian; new
  code, comments, and docstrings are English only.

## ANTI-PATTERNS (THIS PROJECT)

- Editing `kiro/static/**` by hand — it is `frontend/` build output
  (`frontend/vite.config.ts` sets `outDir: "../kiro/static"`).
- Putting images into `userInputMessageContext`; they belong directly in
  `userInputMessage.images` (`kiro/converters_core.py:463` states the rule, the
  assembly is at `:1343`).
- Emitting `toolResults` without the preceding assistant `toolUses` message.
  Cline/Roo/Cursor send histories that need repair (`converters_core.py:814`).
- Normalizing roles after `ensure_alternating_roles()`; the order is fixed
  (`converters_core.py:1024`).
- Client-specific stream mutilation in the gateway. The removed
  `OPENAI_SINGLE_BLOCK_TOOL_COMPAT` mode dropped reasoning, pre-tool text, and
  parallel tool calls; the real defect is in the client adapter.
- Applying the Claude token-correction coefficient to `prompt_tokens`
  (`kiro/streaming_openai.py:334`).
- Rejecting unknown model names. Kiro is the arbiter, not this gateway. The
  resolver also never suggests a model from another family
  (`model_resolver.py:411`).
- Building the upstream host from region alone. A Builder ID account (SSO OIDC
  with no profile ARN) must go to `q.{region}.amazonaws.com`; everything else
  stays on the Kiro host (`kiro/config.py:578`). Absence of a profile alone is
  not the test, and Builder ID accounts must never receive the global fallback
  profile ARN (`routes_openai.py:268`, `routes_anthropic.py:273`).
- Moving `_current_account_index` on failure. It advances only on success —
  that is the global sticky behavior (`account_manager.py:1009`).
- Letting a burst escalate into a long exclusion. `USER_REQUEST_RATE_EXCEEDED`
  parks an account for `ACCOUNT_RATE_LIMIT_COOLDOWN` (10s, `config.py:507`) and
  leaves the circuit breaker untouched; only `MONTHLY_REQUEST_COUNT`
  quarantines it (6h).
- Assuming cache metadata exists. Kiro emits only `contextUsagePercentage` and a
  credit `meteringEvent`, so `cache_read_input_tokens` stays absent.

## COMMANDS

```bash
python main.py --host 127.0.0.1 --port 8000    # run gateway
pytest -q                                      # full suite (1694 tests, <10s, no network)
pytest -v --tb=short                            # exactly what CI runs
pytest --cov=kiro --cov-report=term             # CI coverage gate (.github/workflows/docker.yml)
cd frontend && bun run build                    # tsc -b + vite build into kiro/static
docker compose -f docker-compose.homelab.yml up -d --build
```

CI (`.github/workflows/docker.yml`) runs the `test` job on Python 3.10 and gates
`build` on it; the build job also Trivy-scans and pushes multi-arch images on
non-PR runs only.

## NOTES

- `main.py` refuses to start when `credentials.json` has no usable account, even
  with `ACCOUNT_SYSTEM=false`; set `KIRO_CLI_DB_FILE` for a standalone run.
  `validate_configuration` (`main.py:205`) skips legacy `.env` checks entirely
  once `credentials.json` exists. Legacy mode always recreates
  `credentials.json` from `.env` (`main.py:417`).
- The OpenAI `developer` role must be folded into the system prompt
  (`converters_openai.py:143`); dropping it makes Kiro answer `REQUEST_BODY_INVALID`.
- "Improperly formed request" is Kiro's catch-all validation error. Treat it as
  a signal to diff the emitted payload, not as a specific cause.
- Truncated upstream turns must not be reported as clean finishes
  (`kiro/stop_reasons.py`).
- The homelab compose mounts the host `kiro-cli` store read-only at
  `/host/kiro-cli` with `SQLITE_READONLY=true`. The gateway must never write it.
- 14 files outside `tests/` exceed 500 lines; `kiro/converters_core.py` (1379)
  and `kiro/streaming_anthropic.py` (1007) are the highest-risk edit sites.
