# PROJECT KNOWLEDGE BASE

**Commit:** bee73b3
**Branch:** main

## OVERVIEW

`kiro-lb` is a FastAPI gateway that exposes Kiro (Amazon Q Developer /
CodeWhisperer) through OpenAI- and Anthropic-compatible APIs, load balances
across a pool of Kiro accounts, and ships a React operations dashboard.
Python 3.12 (`Dockerfile`, CI), httpx, loguru, tiktoken. **AGPL-3.0** — based on
`jwadow/kiro-gateway`; see `LICENSE` and `NOTICE.md` (do not relicense).

## STRUCTURE

```
kiro-lb/
├── main.py                  # App factory, lifespan, CLI, static mounts (818 lines)
├── kiro/                    # Gateway package: 56 modules, 23.9k lines
│   └── static/              # BUILD OUTPUT of frontend/ — never hand-edit
├── frontend/                # Bun + Vite + React 19 dashboard source
├── tests/                   # pytest, 2385 tests; network-blocked by conftest
├── data/                    # Unified private dashboard.sqlite3 store (gitignored)
├── deploy/                  # Grafana dashboard + Pushgateway units for /metrics
├── debug_logs/              # Capture output when DEBUG_MODE is on (gitignored)
├── pyproject.toml           # ruff + mypy config only; the project is not packaged
├── docker-compose.yml       # Default deployment (service kiro-lb)
├── docker-compose.homelab.yml  # Live: edge HAProxy :8000 + kiro-blue/green slots
├── docker/haproxy-edge.cfg.template  # rendered → haproxy-edge.generated.cfg
├── deploy/bluegreen/        # zero-downtime deploy.sh (nginx-fixed lab IP)
└── manual_api_test.py       # Manual live-API script, excluded from pytest
```

Root docs: `README.md`, `LICENSE` (AGPL-3.0), `NOTICE.md` (upstream attribution).
Operational detail lives in this file; do not relicense away from AGPL-3.0.

## WHERE TO LOOK

| Task | Location | Notes |
|---|---|---|
| Add/modify a client endpoint | `kiro/routes_openai.py`, `kiro/routes_anthropic.py` | Only 5 + 2 public routes exist |
| Responses API (Codex CLI) | `kiro/routes_openai.py` `/v1/responses` | Facade over `chat_completions`; translation in `converters_responses.py`, `streaming_responses.py` |
| Request -> Kiro payload | `kiro/converters_core.py` | 1508 lines; both adapters delegate here |
| Kiro -> client stream | `kiro/streaming_openai.py`, `kiro/streaming_anthropic.py` | Shared event model in `kiro/streaming_core.py` |
| AWS event-stream framing | `kiro/parsers.py` | Frame reassembly + bracket tool-call recovery |
| Stream order invariants | `kiro/sse_validation.py` | Raises mid-stream instead of shipping bad order |
| Account failover / rotation | `kiro/account_manager.py` | Circuit breaker, quota-weighted selection, lazy init |
| Credentials + host selection | `kiro/auth.py`, `kiro/config.py` | 4 sources; Builder ID routes to a different host |
| Add an account by browser login | `kiro/device_login.py` | Social + Builder ID device flows |
| Shared SQLite persistence | `kiro/store.py` | Accounts, runtime state, internal login credentials; mode 0600/WAL |
| Dashboard API | `kiro/dashboard.py` | 19 routes under `/api/dashboard`, plus `/` and `/metrics` |
| Prometheus exposition | `kiro/metrics.py` | Pure renderer; the route and its auth live in `dashboard.py` |
| Grafana dashboard / metrics wiring | `deploy/` | Dashboard JSON is asserted against the exporter by `tests/unit/test_dashboard_provisioning.py` |
| Token accounting (key/account/model) | `kiro/usage_tracking.py` | Two ContextVar identities, batched flush |
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
| `AccountManager.get_next_account` | method | `kiro/account_manager.py:727` | Quota-weighted candidate order; skips quota-exhausted accounts |
| `AccountManager._weighted_candidate_order` | method | `kiro/account_manager.py` | Weighted sampling by remaining quota fraction; no persisted cursor |
| `AccountManager.report_failure` | method | `kiro/account_manager.py:914` | Classifies 429 / 402 / INVALID_MODEL_ID separately |
| `KiroAuthManager` | class | `kiro/auth.py:67` | Token lifecycle, region + host resolution |
| `ModelResolver` | class | `kiro/model_resolver.py:242` | normalize -> cache -> hidden -> passthrough |
| `resolve_token_profile` | function | `kiro/tokenizer.py:87` | Model name -> (encoding, CJK correction) |
| `validate_live_openai_payload` | function | `kiro/sse_validation.py:196` | Fails the stream instead of shipping bad order |
| `identify_data_api_key` | function | `kiro/dashboard.py:298` | Legacy env key -> `ROOT_KEY_ID`, else hashed `klb_` key |
| `record_token_usage` | function | `kiro/usage_tracking.py` | Attributes tokens to the calling key **and** the serving account |
| `start_device_login` | function | `kiro/device_login.py:299` | Social vs Builder ID flows, deliberately unshared |
| `get_kiro_api_host` / `get_kiro_q_host` | function | `kiro/config.py:613`, `:619` | Runtime generation host vs legacy Builder ID Q host |

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
- `/v1/responses` is a translation facade, not a third pipeline. It builds a
  `ChatCompletionRequest` and calls `chat_completions()` directly, so failover,
  payload building and token accounting are inherited. It translates the
  *serialized* chat chunks rather than tapping the generator: the chunk-order
  validator in `sse_validation.py` is keyed to `begin_openai_stream()` inside that
  generator, and pushing Responses payloads through the same emitter would trip it
  on every event. A route that calls a handler directly must declare its own
  `Depends(verify_api_key)` - FastAPI's dependencies belong to the route, not the
  function.
- The Codex CLI declares its tools in an `additional_tools` *input item*, not in
  `tools`, nested inside a `namespace` entry, and its shell is a `custom`
  (grammar-constrained) tool. Kiro tool specs need a JSON schema, so a freeform
  tool is bridged as a function with one string field and unwrapped back into a
  `custom_tool_call` item (`converters_responses.FREEFORM_BODY_FIELD`). Dropping it
  instead left the model with no tool, and it answered by inventing the command
  output it could not go and fetch.
- Each protocol's usage object carries only the fields that protocol defines.
  OpenAI adds `credits_used` as its one vendor extension; Anthropic's
  `message_delta` restates `input_tokens` only when the value came from upstream
  context usage (`streaming_anthropic.py:684`), and omits it otherwise rather
  than dressing the local estimate up as a correction.
- Token counting picks the encoding per model family (`tokenizer.py:75`):
  `cl100k_base` for Claude and unknown names, `o200k_base` for GPT/o1/o3 and for
  deepseek/qwen/minimax/glm. The correction is a property of the script, not the
  model — it scales by measured CJK ratio and is 1.0 for Latin text.
- Per-key token rows are keyed by the **normalized** model name
  (`usage_tracking.py:33`). Callers pass whatever the client sent, so
  `claude-sonnet-4-5`, the dotted form and a dated form are one model; storing
  them raw split a single model's totals across rows nothing could rejoin.
- Generation throughput divides `timed_completion_tokens` by `generation_ms`,
  never the full `completion_tokens`. The two counters must cover the same
  requests: rows predating the timing column hold tokens with no duration, and
  dividing the full total by a partial duration reported 82,752 tok/s on the live
  store. The exporter pairs them as `kiro_lb_timed_output_tokens_total` and
  `kiro_lb_generation_seconds_total`, both keyed by model only.
- Generation time is measured inside the streaming generators
  (`streaming_openai.py`, `streaming_anthropic.py`), not by the request-log
  middleware. The middleware stops when the handler returns, which for a stream
  is first-byte time: one model averaged 17ms there while generating for seconds,
  so that latency can never be a throughput denominator.
- Frontend logic that a panel derives (totals, chart slices) lives in a plain
  module beside the component, not exported from the `.tsx`: eslint's
  `react-refresh/only-export-components` rejects mixed exports, and it is what
  makes the logic unit-testable (`token-slices.ts`, `format.ts`).
- Control and data planes stay separate: dashboard cookie sessions cannot call
  `/v1`, and `/v1` API keys cannot call `/api/dashboard`. `/metrics` is a third
  plane: it takes a `/v1` bearer key (a scraper cannot hold a cookie) and refuses
  dashboard sessions.
- The private SQLite store contains dashboard metadata plus account policy,
  runtime state, and upstream refresh credentials for internal device logins;
  it never stores prompts, completions, or raw client API keys. New columns arrive via additive `PRAGMA table_info` +
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
- Measuring the payload guard as UTF-8 bytes of the JSON. Upstream
  `CONTENT_LENGTH_EXCEEDS_THRESHOLD` tracks cl100k tokens of the compact JSON
  (claude-opus-5: 800k Hangul pass / 1M fail on runtime.kiro.dev, 2026-08-23).
  (`payload_guards.py` `check_payload_tokens`).
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
- Sending Builder ID generation to `q.{region}.amazonaws.com` or the legacy
  `/generateAssistantResponse` path. Kiro CLI 2.19.1 sends AWS JSON RPC to
  `runtime.{region}.kiro.dev/` and includes its request-scoped Builder ID
  fallback profile. Keep that fallback out of persisted credentials: it is a
  service routing value, not the account's own profile ARN.
- Treating `_current_account_index` as the selection cursor. Routing is
  quota-weighted (`ACCOUNT_QUOTA_WEIGHTED_ROUTING`); the index only records the
  last success and drives the legacy sticky rollback path. It still advances
  only on success.
- Weighting account selection by absolute remaining quota, or letting a weight
  exclude an account. The weight is the remaining *fraction*, and every account
  keeps a nonzero chance — a zero-chance weight is how the sticky cursor starved
  a healthy account in the first place. Exclusion is the health policy's job.
- Persisting `quota_headroom`, `quota_resets_at`, or `quota_overage_enabled` with
  runtime state. They are re-seeded from the usage rows on load
  (`store.load_quota_headroom`, `store.load_quota_period`); a stale weight
  misroutes and a stale reset date distorts the quarantine.
- Ending a 402 quota quarantine on a fixed timer. It runs to the reported reset
  (`_quota_quarantine_until`); `ACCOUNT_QUOTA_QUARANTINE` is the floor and the
  no-date fallback only. A fixed window released accounts ~26 days early at
  1000/1000, where they could answer nothing but 402.
- Removing the last-resort pass in `get_next_account`. `quota_depleted` is
  inferred from telemetry, not from an upstream refusal, so it is the one
  exclusion allowed to be wrong: if it empties the pool, the second pass ignores
  it and lets the upstream answer. Returning "no accounts available" because a
  usage poll stalled is worse than returning a 402.
- Letting a burst escalate into a long exclusion. `USER_REQUEST_RATE_EXCEEDED`
  parks an account for `ACCOUNT_RATE_LIMIT_COOLDOWN` (10s, `config.py:540`) and
  leaves the circuit breaker untouched; only `MONTHLY_REQUEST_COUNT`
  quarantines it (6h).
- Assuming cache metadata exists. Kiro emits only `contextUsagePercentage` and a
  credit `meteringEvent`, so `cache_read_input_tokens` stays absent.

## COMMANDS

```bash
# Dev server: run from a worktree only, see LOCAL DEVELOPMENT (steps 4-5)
pytest -q                                      # full suite (2385 tests, ~17s, no network)
pytest -v --tb=short                           # exactly what CI's test job runs
pytest --cov=kiro --cov-report=term            # CI coverage step
ruff format --check --diff . && ruff check .   # CI quality job, python half
mypy                                           # config in pyproject.toml (kiro + main.py)
cd frontend && bun run lint && bun run typecheck && bun run test && bun run build
docker compose -p kiro-lb -f docker-compose.homelab.yml up -d --build
```

The `-p kiro-lb` is required: the live container was created under that project
name, so a checkout directory that differs (for example an old `kiro-lb-python`
clone) makes compose invent another project and hit a `container_name` conflict
instead of recreating.

CI (`.github/workflows/docker.yml`) has three jobs: `quality` (ruff format check,
ruff check, mypy, frontend eslint + tsc + vitest), `test` (pytest, then coverage), and
`build`, which needs both. The build job Trivy-scans (report-only) and pushes
multi-arch images on non-PR runs. Tool versions are pinned in
`requirements-dev.txt` so a tool release cannot turn CI red on its own.

## LOCAL DEVELOPMENT

The main checkout (`~/github.com/minpeter/kiro-lb`) **is the production
directory**: the live slot bind-mounts its `./data`, reads its `.env`, and
`deploy.sh` flips from it. Never run `python main.py` there. Develop in a
separate git worktree so the store, secrets and slot file are physically apart.

1. Create the worktree from the main checkout, one per branch:

   ```bash
   BRANCH=feat/my-change
   WT="../kiro-lb-worktrees/${BRANCH##*/}"   # directory = branch name minus its prefix
   git fetch origin
   mkdir -p ../kiro-lb-worktrees
   git worktree add --no-track "$WT" -b "$BRANCH" origin/main
   cd "$WT"
   ```

   `--no-track` keeps `origin/main` from becoming the branch's upstream;
   without it a bare `git push` is refused for the name mismatch. Publish the
   branch with `git push -u origin HEAD`.

   All worktrees live in the sibling `kiro-lb-worktrees/` directory, named
   after their branch, so `ls ../kiro-lb-worktrees` is the list of work in
   flight and nothing lands inside the production checkout. Other projects in
   this parent directory follow the same `<repo>-worktrees/` convention.

   `.env`, `data/`, `debug_logs/`, `.venv/` and `frontend/node_modules/` are
   gitignored, so the new tree starts with none of them. Move a worktree with
   `git worktree move`, never `mv`, and recreate `.venv` afterwards: its
   scripts carry the old absolute path in their shebang.

2. Write a dev-only `.env` (never copy the production one):

   ```bash
   cat > .env <<EOF
   PROXY_API_KEY="dev-$(openssl rand -hex 16)"
   DASHBOARD_PASSWORD="dev-$(openssl rand -hex 8)"
   DASHBOARD_DATA_DIR="data"
   DASHBOARD_SECURE_COOKIE="false"
   LOG_LEVEL="DEBUG"
   EOF
   chmod 600 .env
   ```

   Host and port go on the command line (step 4), not here: pytest reads this
   `.env` too, and `SERVER_HOST`/`SERVER_PORT` in it fail the two
   default-value tests in `tests/unit/test_config.py`.

   Leave `KIRO_SLOT` and `HANDOFF_SECRET` unset: without a slot the process
   is the store's sole writer, which is correct for its own `data/` and
   exactly what must never happen against production's.

3. Install dependencies (Python 3.12, matching CI and the Dockerfile):

   ```bash
   uv venv --python 3.12 .venv
   uv pip install --python .venv/bin/python \
     -r requirements.txt -r requirements-test.txt -r requirements-dev.txt
   (cd frontend && bun install --frozen-lockfile)
   ```

4. Run the backend with a clean environment, reachable from other devices on
   the LAN:

   ```bash
   # The host's address on the default-route interface (the LAN). Override by
   # exporting DEV_HOST first. Set it in every terminal you start a server from.
   DEV_HOST="${DEV_HOST:-$(ip -4 -o addr show dev "$(ip -4 route show default \
     | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')" \
     | awk '{split($4,a,"/"); print a[1]; exit}')}"
   echo "$DEV_HOST"   # must print the LAN address, not empty

   env -i HOME="$HOME" PATH="$PATH" .venv/bin/python main.py --host "$DEV_HOST" --port 8100
   ```

   Dev servers always bind the LAN address so other devices on that subnet can
   reach them. Do not use `127.0.0.1` (this host only) or `0.0.0.0` (also
   exposes the server on the VPN and every Docker bridge). Derive the address
   as above; never write a literal IP into the command, `.env` or config.
   Binding picks the interface, it is not a firewall: anything that can route
   to that address can reach the port, so the dev `PROXY_API_KEY` and
   `DASHBOARD_PASSWORD` are the only gates. Keep them random and distinct
   from production.

   Port 8100 stays clear of the production edge (`:8000`) and both slots
   (`127.0.0.1:8001`/`8002`). The session cookie must not be `Secure` over
   plain HTTP; `_secure_cookie()` (`dashboard.py`) already infers that from
   the scheme, and `DASHBOARD_SECURE_COOKIE="false"` from step 2 only pins it
   so a stray `X-Forwarded-Proto: https` cannot break login. Two
   worktrees running at once need their own pair of ports (e.g. 8110/5184);
   `--strictPort` makes Vite fail instead of silently taking the next one.

   `load_dotenv()` (`config.py:15`) never overrides variables already in the
   process environment. A shell that exports `PROXY_API_KEY` or
   `DASHBOARD_PASSWORD` (the operator's does) silently wins over the dev
   `.env`, and the dev server then accepts the production key. `env -i` is the
   guard; check with `curl -H "Authorization: Bearer $PROXY_API_KEY"
   "http://$DEV_HOST:8100/v1/models"` → must be 401.

5. Run the dashboard with HMR in a second terminal:

   ```bash
   cd frontend   # DEV_HOST set as in step 4
   API_PROXY_TARGET="http://$DEV_HOST:8100" \
     bun run dev --host "$DEV_HOST" --port 5174 --strictPort
   ```

   Open `http://$DEV_HOST:5174` from any device on the LAN, by IP: Vite's
   host check answers 403 to any other hostname unless it is added to
   `server.allowedHosts`. Without `--host`
   Vite binds `localhost` only. The backend no longer listens on loopback, so
   the proxy target must be `$DEV_HOST` too. `/api`, `/v1` and `/health` are
   proxied to the dev backend. Without `API_PROXY_TARGET` the proxy defaults
   to `localhost:8000`, which is not the dev server.

6. Add accounts through the dev dashboard's device login, using a Kiro
   account that is **not** in the production pool. The server starts with an
   empty pool (only `PROXY_API_KEY` is mandatory, `main.py:203`). Do not copy
   production `data/dashboard.sqlite3`: it carries upstream refresh tokens, and
   a refresh from the dev copy rotates the credential out from under the live
   slot.

7. Verify before pushing, with the same commands CI runs (see COMMANDS),
   prefixed with `env -i HOME="$HOME" PATH="$PATH"` and using `.venv/bin/`
   tools for the same reason as step 4. If `frontend/` changed, commit the
   `bun run build` output in `kiro/static/` too; it is tracked and the image
   serves it as-is.

8. Push the branch and open a PR against `main`. CI must be green before merge.

9. Deploy from the **main checkout** after merge, never from a worktree:

   ```bash
   cd ~/github.com/minpeter/kiro-lb
   git pull --ff-only
   ./deploy/bluegreen/deploy.sh --status
   ./deploy/bluegreen/deploy.sh
   ```

   `deploy.sh` resolves `data/`, `.env` and `active_slot` relative to its own
   checkout, so running it from a worktree would boot a slot against the dev
   store.

10. Clean up when the branch is merged. Stop that worktree's dev servers
    first, then:

    ```bash
    cd ~/github.com/minpeter/kiro-lb
    git pull --ff-only                        # so `branch -d` sees the merge
    git worktree remove ../kiro-lb-worktrees/my-change
    git branch -d feat/my-change
    ```

    `git worktree remove` refuses only on tracked changes. Ignored files go
    with the tree without a prompt: the dev `.env`, and `data/` with the dev
    accounts' refresh tokens. Copy them out first to reuse them.

## NOTES

- `/metrics` reads only what the gateway already stores (dashboard SQLite + live
  pool), so a scrape cannot perturb routing or spend upstream quota. The homelab
  does not scrape it directly: no job in `/opt/monitoring/prometheus.yml` carries
  credentials, so a workstation timer fetches it with a bearer key and `PUT`s to
  Pushgateway (`job=kiro-lb-usage`, `instance=ws`), which the existing
  `pushgateway` job scrapes with `honor_labels: true`. `job` and `instance` are
  therefore never set in the exposition itself. The tracked copies of those
  units, and the Grafana dashboard, live in `deploy/`; the deployed units are
  symlinked from `~/homelab/kiro-lb-probe/`.
- The Grafana dashboard is checked in (`deploy/grafana/kiro-lb.json`, uid
  `kiro-lb`) so a metric rename cannot silently empty a panel:
  `tests/unit/test_dashboard_provisioning.py` asserts every metric and label it
  queries against `_FAMILIES`, and pins the two constraints that are invisible in
  the UI — rate windows must span several 30s pushes, and latency is a summary
  without quantiles so no p95 panel may claim otherwise. Edit the JSON in the
  repo and copy it out, never only on the monitoring host.
- Grafana on `apps` serves 12 unrelated dashboards, so install with the
  provisioning reload API (`POST /api/admin/provisioning/dashboards/reload`)
  rather than restarting the container.
- `/metrics` is not recorded by the request-metrics middleware (`main.py:623`
  filters to `/v1/`), so scraping does not inflate the counters it reports.
- `main.py` starts with an empty account pool; accounts are added through the
  dashboard's device login. `validate_configuration` (`main.py:203`) only opens
  the store and requires `PROXY_API_KEY`.
- The OpenAI `developer` role must be folded into the system prompt
  (`converters_openai.py:149`); dropping it makes Kiro answer `REQUEST_BODY_INVALID`.
- The oversize rejection is `CONTENT_LENGTH_EXCEEDS_THRESHOLD`, which names
  neither the size nor the limit; `PayloadTooLargeError` fails locally instead so
  both numbers reach the caller. The unit is cl100k tokens, not bytes.
  "Improperly formed request" is Kiro's separate catch-all validation error —
  treat it as a signal to diff the emitted payload.
- The Docker image bakes the tiktoken vocabularies at build time
  (`TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache`). Without them a network-restricted
  container silently degrades to character-based estimation.
- Truncated upstream turns must not be reported as clean finishes
  (`kiro/stop_reasons.py`).
- 19 files outside `tests/` exceed 500 lines; `kiro/converters_core.py` (1508)
  and `kiro/account_manager.py` (1867) are the highest-risk edit sites.
