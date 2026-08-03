# PROJECT KNOWLEDGE BASE

**Commit:** 474df2b
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
├── main.py                  # App factory, lifespan, CLI, static mounts (822 lines)
├── kiro/                    # Gateway package: 39 modules, 16.5k lines
│   └── static/              # BUILD OUTPUT of frontend/ — never hand-edit
├── frontend/                # Bun + Vite + React 19 dashboard source
├── tests/                   # pytest, 1768 tests; network-blocked by conftest
├── data/                    # Runtime credentials/state/sqlite (gitignored)
├── debug_logs/              # Capture output when DEBUG_MODE is on (gitignored)
├── pyproject.toml           # ruff + mypy config only; the project is not packaged
├── docker-compose.yml       # Upstream-style deployment (service kiro-gateway)
├── docker-compose.homelab.yml  # This operator's live deployment (bound to 10.10.10.10)
└── manual_api_test.py       # Manual live-API script, excluded from pytest
```

No `docs/`, root `README.md`, or license/community docs exist any more: `48e728d`
removed the root docs, `a9fadd7` removed the whole `docs/` tree. The invariants
they held are folded into this file; do not link to them.

## WHERE TO LOOK

| Task | Location | Notes |
|---|---|---|
| Add/modify a client endpoint | `kiro/routes_openai.py`, `kiro/routes_anthropic.py` | Only 4 + 2 public routes exist |
| Request -> Kiro payload | `kiro/converters_core.py` | 1363 lines; both adapters delegate here |
| Kiro -> client stream | `kiro/streaming_openai.py`, `kiro/streaming_anthropic.py` | Shared event model in `kiro/streaming_core.py` |
| AWS event-stream framing | `kiro/parsers.py` | Frame reassembly + bracket tool-call recovery |
| Stream order invariants | `kiro/sse_validation.py` | Raises mid-stream instead of shipping bad order |
| Account failover / rotation | `kiro/account_manager.py` | Circuit breaker, global sticky, lazy init |
| Credentials + host selection | `kiro/auth.py`, `kiro/config.py` | 4 sources; Builder ID routes to a different host |
| Add an account by browser login | `kiro/device_login.py` | Social + Builder ID device flows |
| Dashboard API / SQLite store | `kiro/dashboard.py` | 18 routes under `/api/dashboard`, plus `/` and `/metrics` |
| Prometheus exposition | `kiro/metrics.py` | Pure renderer; the route and its auth live in `dashboard.py` |
| Per-key token accounting | `kiro/usage_tracking.py` | ContextVar identity, batched flush |
| Token counting / encodings | `kiro/tokenizer.py` | Per-family encoding + CJK-only correction |
| Model name handling | `kiro/model_resolver.py` | Never raises; unknown names pass through |
| Model list + token limits | `kiro/routes_openai.py:203`, `kiro/config.py:294` | One superset response; limits from live API or `FALLBACK_MODELS` |
| Request/response schemas | `kiro/models_openai.py`, `kiro/models_anthropic.py` | Extra-open models preserve unknown fields |
| Extended thinking budgets | `kiro/native_thinking.py` | Budget must be >= 1024; unknown fields rejected upstream |
| Failure capture/replay | `kiro/debug_capture.py`, `kiro/debug_replay.py`, `kiro/debug_sanitize.py` | Redacted by default |
| Payload size guard | `kiro/payload_guards.py` | Measured cutoff: 1,085,435 bytes pass, 1,086,459 fail (`config.py:482`) |

## CODE MAP

| Symbol | Type | Location | Role |
|---|---|---|---|
| `build_kiro_payload` | function | `kiro/converters_core.py:1185` | Single funnel for every upstream request |
| `parse_kiro_stream` | async gen | `kiro/streaming_core.py:119` | Only place raw frames become `KiroEvent` |
| `KiroEvent` | dataclass | `kiro/streaming_core.py:51` | Protocol-neutral event both adapters consume |
| `AccountManager` | class | `kiro/account_manager.py:260` | Pool state, failover, rate series, persistence |
| `AccountManager.get_next_account` | method | `kiro/account_manager.py:727` | Rotation from the global index; skips quota-exhausted accounts |
| `AccountManager.report_failure` | method | `kiro/account_manager.py:914` | Classifies 429 / 402 / INVALID_MODEL_ID separately |
| `KiroAuthManager` | class | `kiro/auth.py:67` | Token lifecycle, region + host resolution |
| `ModelResolver` | class | `kiro/model_resolver.py:242` | normalize -> cache -> hidden -> passthrough |
| `resolve_token_profile` | function | `kiro/tokenizer.py:87` | Model name -> (encoding, CJK correction) |
| `validate_live_openai_payload` | function | `kiro/sse_validation.py:196` | Fails the stream instead of shipping bad order |
| `identify_data_api_key` | function | `kiro/dashboard.py:298` | Legacy env key -> `ROOT_KEY_ID`, else hashed `klb_` key |
| `record_token_usage` | function | `kiro/usage_tracking.py:30` | Attributes tokens to the calling key |
| `start_device_login` | function | `kiro/device_login.py:299` | Social vs Builder ID flows, deliberately unshared |
| `get_kiro_api_host` / `get_kiro_q_host` | function | `kiro/config.py:613`, `:619` | Builder ID flag picks the host template |

Line numbers drift on every edit to these files. Grep the symbol name before
trusting a pin here.

## CONVENTIONS

- Reasoning is forwarded only from native upstream frames. Never synthesize it
  from response text or prompt tags.
- OpenAI reasoning is emitted as `reasoning` (`streaming_openai.py:199`), not the
  legacy `reasoning_content`; requests accept both on input.
- Streaming uses a per-request `httpx.AsyncClient`; non-streaming uses the shared
  pooled client from `lifespan` (`main.py:346`). Reusing the shared client for
  streams leaks CLOSE_WAIT.
- Any new client-visible behavior must land on OpenAI **and** Anthropic, in both
  streaming and non-streaming paths.
- Each protocol's usage object carries only the fields that protocol defines.
  OpenAI adds `credits_used` as its one vendor extension; Anthropic's
  `message_delta` restates `input_tokens` only when the value came from upstream
  context usage (`streaming_anthropic.py:684`), and omits it otherwise rather
  than dressing the local estimate up as a correction.
- Token counting picks the encoding per model family (`tokenizer.py:75`):
  `cl100k_base` for Claude and unknown names, `o200k_base` for GPT/o1/o3 and for
  deepseek/qwen/minimax/glm. The correction is a property of the script, not the
  model — it scales by measured CJK ratio and is 1.0 for Latin text.
- Control and data planes stay separate: dashboard cookie sessions cannot call
  `/v1`, and `/v1` API keys cannot call `/api/dashboard`. `/metrics` is a third
  plane: it takes a `/v1` bearer key (a scraper cannot hold a cookie) and refuses
  dashboard sessions.
- Dashboard SQLite stores metadata only — no prompts, completions, raw keys, or
  refresh tokens. New columns arrive via additive `PRAGMA table_info` +
  `ALTER TABLE` migration (`dashboard.py:96`, `:147`), never a destructive rewrite.
- Proxy env vars are set before any httpx client exists (`main.py:181`); creating
  a client earlier silently ignores the VPN/proxy config.
- `web_search` auto-injection (Path B) is opt-in and off by default
  (`config.py:498`): injecting a tool the caller never asked for changes the
  shape of every request. Native server-side `web_search` (Path A) is driven by
  the client and works regardless of the flag (`routes_anthropic.py:173`).
- `pytest.ini`, `kiro/__init__.py`, and legacy comments in `kiro/http_client.py`,
  `kiro/mcp_tools.py` and 5 test modules are Russian; new code, comments, and
  docstrings are English only.

## ANTI-PATTERNS (THIS PROJECT)

- Editing `kiro/static/**` by hand — it is `frontend/` build output
  (`frontend/vite.config.ts` sets `outDir: "../kiro/static"`).
- Putting images into `userInputMessageContext`; they belong directly in
  `userInputMessage.images` (`kiro/converters_core.py:453` states the rule, the
  assembly is at `:1321`).
- Emitting `toolResults` without the preceding assistant `toolUses` message.
  Cline/Roo/Cursor send histories that need repair (`converters_core.py:795`).
- Normalizing roles after `ensure_alternating_roles()`; the order is fixed
  (`converters_core.py:1001`).
- Client-specific stream mutilation in the gateway. The removed
  `OPENAI_SINGLE_BLOCK_TOOL_COMPAT` mode dropped reasoning, pre-tool text, and
  parallel tool calls; the real defect is in the client adapter.
- Inventing a usage field. `context_usage_percentage` was emitted on both
  protocols for one commit and reverted (`da559c5`): its only purpose is being
  converted into a token count, and that conversion already runs.
- Applying the Claude token-correction coefficient to `prompt_tokens`
  (`kiro/streaming_openai.py:343`) or as a blanket multiplier on Latin text.
- Measuring the payload guard with `json.dumps` defaults. `check_payload_size`
  uses `ensure_ascii=False` because that is what the routes send; with escapes a
  Hangul character measures 6 bytes instead of 3 and Korean conversations were
  rejected at half the accepted size (`payload_guards.py:52`).
- Trusting the advertised context window. `claude-opus-4.7`, `claude-opus-4.8`,
  `claude-opus-5` and `claude-sonnet-5` report 1000000 but charge against 666667;
  `FALLBACK_MODELS` (`config.py:294`) deliberately carries the measured value.
- Registering a second `/v1/models` route. Both routers mount on the same app, so
  the second registration is shadowed; the one response is an OpenAI + Anthropic
  superset (`models_openai.py:20`).
- Rejecting unknown model names. Kiro is the arbiter, not this gateway. The
  resolver also never suggests a model from another family
  (`model_resolver.py:405`).
- Labelling a Prometheus series with a raw model name. The resolver forwards
  unknown names to Kiro, so `model` is client-controlled: the live store holds 40
  distinct names including probes like `claude-opus-99`. `kiro/metrics.py`
  normalizes, then clamps to what the pool serves and collapses the rest into
  `other`, re-aggregating in Python so no series is emitted twice.
- Building the upstream host from region alone. A Builder ID account (SSO OIDC
  with no profile ARN) must go to `q.{region}.amazonaws.com`; everything else
  stays on the Kiro host (`kiro/config.py:613`). Absence of a profile alone is
  not the test, and Builder ID accounts must never receive the global fallback
  profile ARN (`routes_openai.py:371`, `routes_anthropic.py:255`).
- Moving `_current_account_index` on failure. It advances only on success —
  that is the global sticky behavior (`account_manager.py:999`).
- Letting a burst escalate into a long exclusion. `USER_REQUEST_RATE_EXCEEDED`
  parks an account for `ACCOUNT_RATE_LIMIT_COOLDOWN` (10s, `config.py:540`) and
  leaves the circuit breaker untouched; only `MONTHLY_REQUEST_COUNT`
  quarantines it (6h).
- Assuming cache metadata exists. Kiro emits only `contextUsagePercentage` and a
  credit `meteringEvent`, so `cache_read_input_tokens` stays absent.

## COMMANDS

```bash
python main.py --host 127.0.0.1 --port 8000    # run gateway
pytest -q                                      # full suite (1768 tests, ~6s, no network)
pytest -v --tb=short                           # exactly what CI's test job runs
pytest --cov=kiro --cov-report=term            # CI coverage step
ruff format --check --diff . && ruff check .   # CI quality job, python half
mypy                                           # config in pyproject.toml (kiro + main.py)
cd frontend && bun run lint && bun run typecheck && bun run build
docker compose -p kiro-lb -f docker-compose.homelab.yml up -d --build
```

The `-p kiro-lb` is required: the live container was created under that project
name, so letting compose derive it from the directory (`kiro-lb-python`) fails on
a `container_name` conflict instead of recreating.

CI (`.github/workflows/docker.yml`) has three jobs: `quality` (ruff format check,
ruff check, mypy, frontend eslint + tsc), `test` (pytest, then coverage), and
`build`, which needs both. The build job Trivy-scans (report-only) and pushes
multi-arch images on non-PR runs. Tool versions are pinned in
`requirements-dev.txt` so a tool release cannot turn CI red on its own.

## NOTES

- `/metrics` reads only what the gateway already stores (dashboard SQLite + live
  pool), so a scrape cannot perturb routing or spend upstream quota. The homelab
  does not scrape it directly: no job in `/opt/monitoring/prometheus.yml` carries
  credentials, so a workstation timer fetches it with a bearer key and `PUT`s to
  Pushgateway (`job=kiro-lb-usage`, `instance=ws`), which the existing
  `pushgateway` job scrapes with `honor_labels: true`. `job` and `instance` are
  therefore never set in the exposition itself. Units live in
  `~/homelab/kiro-lb-probe/`.
- `/metrics` is not recorded by the request-metrics middleware (`main.py:623`
  filters to `/v1/`), so scraping does not inflate the counters it reports.
- `main.py` refuses to start when `credentials.json` has no usable account, even
  with `ACCOUNT_SYSTEM=false`; set `KIRO_CLI_DB_FILE` for a standalone run.
  `validate_configuration` (`main.py:201`) skips legacy `.env` checks entirely
  once `credentials.json` exists. Legacy mode always recreates
  `credentials.json` from `.env` (`main.py:401`).
- The OpenAI `developer` role must be folded into the system prompt
  (`converters_openai.py:149`); dropping it makes Kiro answer `REQUEST_BODY_INVALID`.
- The oversize rejection is `CONTENT_LENGTH_EXCEEDS_THRESHOLD`, which names
  neither the size nor the limit; `PayloadTooLargeError` fails locally instead so
  both numbers reach the caller. "Improperly formed request" is Kiro's separate
  catch-all validation error — treat it as a signal to diff the emitted payload.
- The Docker image bakes the tiktoken vocabularies at build time
  (`TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache`). Without them a network-restricted
  container silently degrades to character-based estimation.
- Truncated upstream turns must not be reported as clean finishes
  (`kiro/stop_reasons.py`).
- The homelab compose mounts the host `kiro-cli` store read-only at
  `/host/kiro-cli` with `SQLITE_READONLY=true`. The gateway must never write it.
- 13 files outside `tests/` exceed 500 lines; `kiro/converters_core.py` (1363)
  and `kiro/account_manager.py` (1252) are the highest-risk edit sites.
