# PROJECT KNOWLEDGE BASE

**Commit:** b9c2336
**Branch:** main

## OVERVIEW

`kiro-lb` is a FastAPI reverse-engineered proxy that exposes Kiro (Amazon Q
Developer / CodeWhisperer) through OpenAI- and Anthropic-compatible APIs, load
balances across a pool of Kiro accounts, and ships a private React operations
dashboard. Python 3.10+, httpx, loguru, tiktoken. AGPL-3.0, partly derived from
Kiro Gateway (`jwadow/kiro-gateway`) — keep the per-file license headers.

## STRUCTURE

```
kiro-lb-python/
├── main.py                  # App factory, lifespan, CLI, static mounts (873 lines)
├── kiro/                    # Gateway package: 39 modules, ~17.1k lines
│   └── static/              # BUILD OUTPUT of frontend/ — never hand-edit
├── frontend/                # Bun + Vite + React 19 dashboard source
├── tests/                   # pytest, 1694 tests; network-blocked by conftest
├── docs/                    # ADRs, router design, translated READMEs
├── data/                    # Runtime credentials/state/sqlite (gitignored)
├── debug_logs/              # Capture output when DEBUG_MODE is on (gitignored)
├── docker-compose.yml       # Upstream-style deployment
├── docker-compose.homelab.yml  # This operator's live deployment (bound to 10.10.10.10)
└── manual_api_test.py       # Manual live-API script, excluded from pytest
```

## WHERE TO LOOK

| Task | Location | Notes |
|---|---|---|
| Add/modify a client endpoint | `kiro/routes_openai.py`, `kiro/routes_anthropic.py` | Only 4 + 2 public routes exist |
| Request -> Kiro payload | `kiro/converters_core.py` | 1397 lines; both adapters delegate here |
| Kiro -> client stream | `kiro/streaming_openai.py`, `kiro/streaming_anthropic.py` | Shared event model in `kiro/streaming_core.py` |
| AWS event-stream framing | `kiro/parsers.py` | Frame reassembly + bracket tool-call recovery |
| Stream order invariants | `kiro/sse_validation.py` | Raises mid-stream instead of shipping bad order |
| Account failover / rotation | `kiro/account_manager.py` | Circuit breaker, global sticky, lazy init |
| Credentials + host selection | `kiro/auth.py`, `kiro/config.py` | 4 sources; Builder ID routes to a different host |
| Add an account by browser login | `kiro/device_login.py` | Social + Builder ID device flows |
| Dashboard API / SQLite store | `kiro/dashboard.py` | 17 routes under `/api/dashboard`, plus `/` |
| Per-key token accounting | `kiro/usage_tracking.py` | ContextVar identity, batched flush |
| Model name handling | `kiro/model_resolver.py` | Never raises; unknown names pass through |
| Failure capture/replay | `kiro/debug_capture.py`, `kiro/debug_replay.py` | Redacted by default |
| Payload size repair | `kiro/payload_guards.py` | Kiro rejects ~615KB+ (`payload_guards.py:23`) |

## CODE MAP

| Symbol | Type | Location | Role |
|---|---|---|---|
| `build_kiro_payload` | function | `kiro/converters_core.py:1224` | Single funnel for every upstream request |
| `parse_kiro_stream` | async gen | `kiro/streaming_core.py:130` | Only place raw frames become `KiroEvent` |
| `KiroEvent` | dataclass | `kiro/streaming_core.py:66` | Protocol-neutral event both adapters consume |
| `AccountManager` | class | `kiro/account_manager.py:276` | Pool state, failover, rate series, persistence |
| `AccountManager.get_next_account` | method | `kiro/account_manager.py:749` | Rotation; excludes quota-exhausted accounts outright |
| `AccountManager.report_failure` | method | `kiro/account_manager.py:936` | Classifies 429 / 402 / INVALID_MODEL_ID separately |
| `KiroAuthManager` | class | `kiro/auth.py:85` | Token lifecycle, region + host resolution |
| `ModelResolver` | class | `kiro/model_resolver.py:259` | normalize -> cache -> hidden -> passthrough |
| `validate_live_openai_payload` | function | `kiro/sse_validation.py:204` | Fails the stream instead of shipping bad order |
| `identify_data_api_key` | function | `kiro/dashboard.py:309` | Legacy env key -> `ROOT_KEY_ID`, else hashed `klb_` key |
| `record_token_usage` | function | `kiro/usage_tracking.py:48` | Attributes tokens to the calling key |
| `start_device_login` | function | `kiro/device_login.py:317` | Social vs Builder ID flows, deliberately unshared |

## CONVENTIONS

- Reasoning is forwarded only from native upstream frames. Never synthesize it
  from response text or prompt tags (`docs/adr/0001-real-upstream-reasoning-only.md`).
- OpenAI reasoning is emitted as `reasoning` (`streaming_openai.py:205`), not the
  legacy `reasoning_content`; requests accept both on input.
- Streaming uses a per-request `httpx.AsyncClient`; non-streaming uses the shared
  pooled client from `lifespan`. Reusing the shared client for streams leaks CLOSE_WAIT.
- Any new client-visible behavior must land on OpenAI **and** Anthropic, in both
  streaming and non-streaming paths.
- Control and data planes stay separate: dashboard cookie sessions cannot call
  `/v1`, and `/v1` API keys cannot call `/api/dashboard` (`docs/router-design.md`).
- Dashboard SQLite stores metadata only — no prompts, completions, raw keys, or
  refresh tokens. New columns arrive via additive `ALTER TABLE` migration
  (`dashboard.py:150`), never a destructive rewrite.
- `pytest.ini`, `kiro/__init__.py`, and some legacy comments are Russian; new
  code, comments, and docstrings are English only.

## ANTI-PATTERNS (THIS PROJECT)

- Editing `kiro/static/**` by hand — it is `frontend/` build output
  (`frontend/vite.config.ts` sets `outDir: "../kiro/static"`).
- Putting images into `userInputMessageContext`; they belong directly in
  `userInputMessage.images` (`kiro/converters_core.py:1177`).
- Client-specific stream mutilation in the gateway. The removed
  `OPENAI_SINGLE_BLOCK_TOOL_COMPAT` mode dropped reasoning, pre-tool text, and
  parallel tool calls; the real defect is in the client adapter.
- Applying the Claude token-correction coefficient to `prompt_tokens`
  (`kiro/streaming_openai.py:352`).
- Rejecting unknown model names. Kiro is the arbiter, not this gateway.
- Building the upstream host from region alone. A Builder ID account (SSO OIDC
  with no profile ARN) must go to `q.{region}.amazonaws.com`; everything else
  stays on `runtime.kiro.dev` (`kiro/config.py:596`). Absence of a profile alone
  is not the test.
- Letting a burst escalate into a long exclusion. `USER_REQUEST_RATE_EXCEEDED`
  parks an account for `ACCOUNT_RATE_LIMIT_COOLDOWN` (10s) and leaves the circuit
  breaker untouched; only `MONTHLY_REQUEST_COUNT` quarantines it (6h).
- Assuming cache metadata exists. Kiro emits only `contextUsagePercentage` and a
  credit `meteringEvent`, so `cache_read_input_tokens` stays absent.

## COMMANDS

```bash
python main.py --host 127.0.0.1 --port 8000    # run gateway
pytest -q                                      # full suite (1694 tests, <10s, no network)
pytest tests/unit/test_streaming_openai.py -v   # focused protocol suite
pytest --cov=kiro --cov-report=term             # what CI measures (.github/workflows/docker.yml)
cd frontend && bun run build                    # rebuild dashboard into kiro/static
docker compose -f docker-compose.homelab.yml up -d --build
```

## NOTES

- `main.py` refuses to start when `credentials.json` has no usable account, even
  with `ACCOUNT_SYSTEM=false`; set `KIRO_CLI_DB_FILE` for a standalone run.
  `validate_configuration` (`main.py:223`) skips legacy `.env` checks entirely
  once `credentials.json` exists.
- The OpenAI `developer` role must be folded into the system prompt
  (`converters_openai.py:161`); dropping it makes Kiro answer `REQUEST_BODY_INVALID`.
- "Improperly formed request" is Kiro's catch-all validation error. Treat it as
  a signal to diff the emitted payload, not as a specific cause.
- The homelab compose mounts the host `kiro-cli` store read-only at
  `/host/kiro-cli` with `SQLITE_READONLY=true`. The gateway must never write it.
- 39 Python files exceed 500 lines (14 outside `tests/`);
  `kiro/converters_core.py` and `kiro/streaming_anthropic.py` are the
  highest-risk edit sites.
- Root `AGENTS.md`, `README.md`, and the license files are currently deleted in
  the working tree but present at HEAD. Check `git status` before assuming a
  root doc is missing.
