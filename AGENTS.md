# PROJECT KNOWLEDGE BASE

## OVERVIEW

`kiro-lb` is a Rust gateway (axum + tokio) that exposes Kiro (Amazon Q
Developer / CodeWhisperer) through OpenAI- and Anthropic-compatible APIs, load
balances across a pool of Kiro accounts, and ships a React operations dashboard
embedded in the binary. **AGPL-3.0**: a Rust rewrite of the Python port of
`jwadow/kiro-gateway`; see `LICENSE` and `NOTICE.md` (do not relicense).

The Python implementation was removed. Its SQLite schema, `/v1` wire format,
`/api/dashboard` JSON, session cookie and scrypt key format are kept
byte-compatible, so an existing `data/dashboard.sqlite3` opens with no migration.

## STRUCTURE

```
kiro-lb/
├── Cargo.toml               # crate kiro-lb, binary kirolb
├── .cargo/config.toml       # static CRT on Windows (no VC++ redistributable)
├── src/                     # gateway: 40 modules, ~15k lines
│   ├── main.rs              # CLI, router, startup, background tasks, banner
│   ├── bootstrap.rs         # first run: writes .env.example + .env with keys
│   ├── upstream/            # HTTP client, retries, endpoint rotation, proxies
│   └── docs.rs              # /docs (Swagger UI) + /openapi.json
├── static/                  # BUILD OUTPUT of frontend/, embedded via include_dir
├── frontend/                # Bun + Vite + React 19 dashboard source
├── tests/golden.rs          # replays tools/golden/corpus recorded from Python
├── tools/golden/corpus/     # frozen Python oracle outputs (*.json.gz)
├── data/                    # dashboard.sqlite3 (gitignored)
├── deploy/                  # Grafana dashboard, Pushgateway units, blue/green
├── Dockerfile               # packages CI-built dist/kirolb-linux-* binaries
└── docker-compose*.yml      # default and homelab (edge HAProxy + 2 slots)
```

## WHERE TO LOOK

| Task | Location | Notes |
|---|---|---|
| Client endpoints | `src/routes_v1.rs` | messages, count_tokens, chat/completions, responses, models |
| Responses API (Codex CLI) | `src/convert_responses.rs`, `src/stream_responses.rs` | Facade over chat completions |
| Request -> Kiro payload | `src/convert_core.rs` | Both adapters delegate here |
| Kiro -> client stream | `src/stream_openai.rs`, `src/stream_anthropic.rs` | Shared events in `src/stream_core.rs` |
| AWS event-stream framing | `src/parser.rs` | Frame reassembly + bracket tool-call recovery |
| Account failover / routing | `src/pool.rs` | Circuit breaker, weighted/sticky/most_credits/session |
| Credentials + hosts | `src/auth.rs`, `src/config.rs` | Builder ID routes to a different host |
| Device login | `src/device_login.rs` | Social + Builder ID flows |
| Browser sign-in | `src/browser_login.rs` | Google/GitHub PKCE via app.kiro.dev, loopback callback on 3128 |
| SQLite persistence | `src/store.rs`, `src/dashboard_store.rs` | WAL, additive migrations |
| Dashboard API | `src/routes_dashboard.rs` | `/api/dashboard/*`, `/metrics`, handoff |
| Runtime settings | `src/settings.rs` | Tunables, endpoint rotation, tool shortening |
| Prometheus exposition | `src/metrics.rs` | Model labels clamped |
| Token accounting | `src/usage_tracking.rs` | Per key, account, model; batched flush |
| Token counting | `src/tokenizer.rs` | Per-family encoding + CJK-only correction |
| Model names | `src/model_resolver.rs` | Never rejects; unknown names pass through |
| Payload guard | `src/payload_guard.rs` | cl100k tokens of the compact JSON |
| Claude Code tool shortening | `src/prompt_filter.rs` | Shorten long tool descriptions and add the Write/Edit note (on by default) |
| Debug capture / replay | `src/debug.rs` | `kirolb replay <capture>` |

## CONVENTIONS

- Reasoning is forwarded only from native upstream frames. Never synthesize it
  from response text or prompt tags.
- OpenAI reasoning is emitted as `reasoning`, not `reasoning_content`; requests
  accept both.
- Any new client-visible behavior lands on OpenAI **and** Anthropic, streaming
  and non-streaming.
- `/v1/responses` is a translation facade over chat completions, not a third
  pipeline: failover, payload building and token accounting are inherited.
- The Codex CLI declares tools in an `additional_tools` input item, and its
  shell is a `custom` freeform tool. It is bridged as a function with one string
  field and unwrapped back into `custom_tool_call`. Dropping it made the model
  invent command output.
- Each protocol's usage object carries only the fields that protocol defines.
- Token counting: `cl100k_base` for Claude and unknown names, `o200k_base` for
  GPT/o-series and deepseek/qwen/minimax/glm. The CJK correction is a property of
  the script and is 1.0 for Latin text.
- Per-key token rows are keyed by the normalized model name.
- Output speed follows the benchmark definition (Artificial Analysis, IETF
  TPOT): `(output_tokens - 1) / (t_last - t_first)` between the first and last
  output event. Time to first token is stored separately (`request_logs.ttft_ms`)
  and never mixed into speed. `generation_ms` is that decode window, and
  `timed_completion_tokens` adds `completion - 1` so the `/metrics` ratio
  matches. Non-streaming responses have no per-token timing and record no speed.
- Control and data planes stay separate: dashboard sessions cannot call `/v1`,
  `/v1` keys cannot call `/api/dashboard`. `/metrics` takes a `/v1` bearer key.
- The SQLite store never holds prompts, completions or raw client keys. New
  columns arrive by additive `PRAGMA table_info` + `ALTER TABLE`.
- Request bodies have no axum size limit (`DefaultBodyLimit::disable`); the
  payload guard is the only size check.
- `web_search` auto-injection (Path B) is opt-in; native server-side
  `web_search` (Path A) always works.
- `load_balancing` defaults to `session`: one conversation stays on one account
  so Kiro's per-account prompt cache stays warm (~1.6s vs 2.4-6s to first byte).
  It falls back to weighted order when that account is unavailable.
- Frontend logic a panel derives lives in a plain module beside the component
  (eslint `react-refresh/only-export-components`). UI strings go through
  `usePreferences().t` with catalogs in `frontend/src/features/dashboard/i18n/`;
  en-US text must stay identical to what tests assert.
- Code, comments and identifiers are English only.

## ANTI-PATTERNS (THIS PROJECT)

- Editing `static/**` by hand: it is `frontend/` build output.
- Putting images into `userInputMessageContext`; they go in
  `userInputMessage.images`.
- Emitting `toolResults` without the preceding assistant `toolUses`.
- Normalizing roles after `ensure_alternating_roles()`.
- Client-specific stream mutilation in the gateway.
- Inventing a usage field (`context_usage_percentage` was reverted).
- Applying the Claude correction coefficient to prompt tokens or to Latin text.
- Measuring the payload guard as UTF-8 bytes. `CONTENT_LENGTH_EXCEEDS_THRESHOLD`
  tracks cl100k tokens of the compact JSON (claude-opus-5: 800k Hangul pass /
  1M fail, 2026-08-23). Image base64 is not part of it: the guard blanks
  `images[].source.bytes` and adds ceil(w*h/750) per image (capped at 1600),
  because upstream only enforces its own 5 MiB per-image cap (2026-09-27).
  Thinking `signature` strings are blanked the same way: they are opaque
  attestations, and one session's signatures measured 794k tokens while
  contextUsage reported ~19% (#93).
- Hoisting a mid-conversation `system`/`developer` message into the system
  prompt. The system prompt is prepended to the first history turn, so a
  message that changes every turn (Claude Code's `<total_tokens>`) changed the
  prompt prefix and defeated Kiro's per-account prompt cache. Only leading
  system messages become the system prompt; later ones stay in place as
  `<system-reminder>` user text.
- Closing idle upstream connections early. Each new TLS connection to Kiro
  costs ~340ms and a reader idles longer than 90s between prompts; the pool
  keeps connections 30 min with an HTTP/2 PING every 20s.
- Shrinking the 1M window to 666667. `contextUsagePercentage` is a share of the
  advertised window for every model; the newer Claude tokenizer spends more
  tokens on Latin text (1.38x English vs Opus 4.6), while digits and Hangul
  measure the same on both generations, which a smaller window would not.
- Dropping the adaptive `thinking` block from Claude reasoning requests. Without
  it `claude-sonnet-5` never streams reasoning and `claude-sonnet-5.5` skips it
  at low effort (2026-10-02).
- Listing dotted Claude ids in `/v1/models`. Anthropic clients key display
  names, effort levels and the 1M badge on the hyphenated id
  (`claude-opus-5-5`); both spellings resolve to the same Kiro id.
- Moving a session pin on a transient failure. A burst limit or a full slot
  lets another account answer that turn; the pin moves only when its account
  is suspended, signed out, out of quota or lacks the model. Pins persist with
  the runtime state and are dropped when expired or bound to another login.
- Rewriting the Claude Code system prompt. The removed condenser also dropped
  the security policy before the first heading and untitled user instructions.
- Counting `count_tokens` or `/v1/models` as traffic. Clients send one
  `count_tokens` per tool, skill and memory file after each turn; the overview
  and `/metrics` traffic series count generation routes only.
- Measuring time to first token from the upstream response. Kiro sends headers
  only when the first token is ready, so it is measured from request arrival.
- Returning an upstream context overflow as a generic error. It goes out in each
  provider's shape (`prompt is too long: N tokens > M maximum`,
  `context_length_exceeded`) so clients compact and retry.
- Failing the stream on a tool call Kiro cut off. Kiro ends a response after a
  few minutes or ~32k output tokens, thinking included, and sends a long tool
  argument only when it is complete; a 500 made Claude Code resend the same
  request in a loop. A cut call is dropped and the turn ends as `max_tokens`.
- Sending `access-control-allow-origin: *` on `/api/dashboard`. Only `/v1`
  answers any origin; with `DASHBOARD_AUTH=false` the dashboard also refuses
  cross-site requests, since no cookie stands in the way.
- Answering a spent pool with 503. Clients retry 5xx; a missing model, an
  exhausted monthly quota or a suspended pool gets 404/402/403 instead.
- Rejecting unknown model names, or suggesting a model from another family.
- Labelling a Prometheus series with a raw model name.
- Sharing one machine id across accounts, or mixing CLI markers into the IDE
  user agent. Headers mirror a captured Kiro IDE (`src/utils.rs`), and each
  account's machine id derives from its login lineage (`KiroAuth::machine_id`).
- Sending Builder ID generation to `q.{region}.amazonaws.com`. Keep the
  request-scoped fallback profile out of persisted credentials.
- Weighting selection by absolute remaining quota, or letting a weight exclude
  an account. Every account keeps a nonzero chance.
- Persisting `quota_headroom`, `quota_resets_at` or `quota_overage_enabled` with
  runtime state; they are re-seeded from usage rows.
- Ending a 402 quarantine on a fixed timer; it runs to the reported reset.
- Removing the last-resort pass in account selection.
- Escalating a burst into a long exclusion: `USER_REQUEST_RATE_EXCEEDED` parks
  an account for 10s; only `MONTHLY_REQUEST_COUNT` quarantines it (6h).
- Assuming cache metadata exists.

## COMMANDS

```bash
cargo run --release -- --port 8000             # run gateway
cargo test                                     # unit + golden corpus
cargo fmt --check && cargo clippy --all-targets -- -D warnings
cd frontend && bun run lint && bun run typecheck && bun run test && bun run build
kirolb replay debug_logs/<capture>             # replay a failure capture
docker compose -p kiro-lb -f docker-compose.homelab.yml up -d --build
```

CI (`.github/workflows/build.yml`):

1. `frontend`: eslint, tsc, vitest, then build `static/`.
2. `test`: fmt, clippy and `cargo test`.
3. `binaries`: builds the release executables:
   - `kirolb-windows-x64.exe` and `kirolb-windows-arm64.exe` (static CRT);
   - `kirolb-linux-x64` and `kirolb-linux-arm64` (musl, statically linked, so they run on any distro).
4. `release`: runs on `v*` tags and attaches the binaries plus `SHA256SUMS` to a GitHub release.
5. `docker`: builds and pushes a multi-arch image from the Linux binaries.

The Docker image needs `dist/kirolb-linux-*`, so build it from CI artifacts.

## NOTES

- First run with no `.env`: `src/bootstrap.rs` writes `.env.example` (embedded
  in the binary) and a `.env` with a random `PROXY_API_KEY` and
  `DASHBOARD_PASSWORD`, then prints them. An existing `.env` is never touched.
- The gateway starts with an empty store and warns; add the first account from
  the dashboard. `/v1` answers 503 until then.
- tiktoken vocabularies are compiled into the binary; no network is needed to
  count tokens.
- `/metrics` reads only stored data, so a scrape cannot spend upstream quota.
  The homelab pushes it to Pushgateway from a workstation timer (`deploy/`).
- `deploy/grafana/kiro-lb.json` is the checked-in dashboard; edit it in the repo.
- `tools/golden/corpus` is the frozen Python oracle. The recorder was removed
  with the Python code, so the corpus can no longer be regenerated; extend Rust
  unit tests instead.
