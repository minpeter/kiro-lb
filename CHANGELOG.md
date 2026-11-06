# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- honour `text.format` on `/v1/responses` so Codex thread titles parse (#128)

## [0.2.12](https://github.com/minpeter/kiro-lb/compare/v0.2.11...v0.2.12) - 2026-11-05

### Added

- add IAM Identity Center dashboard login

### Fixed

- resume organization discovery after a cancelled poll
- keep organization logins recoverable and translate their errors

### Other

- Merge main into Responses namespace fix

### Added

- dashboard sign-in for organization accounts (IAM Identity Center, #116): enter the organization's start URL and SSO region, approve the device code, and pick a Kiro profile when the organization has more than one (a single profile is chosen automatically). The account refreshes through its SSO region and calls the API in its profile's region. Implemented by @madrobotnet (#120), proposed by @yelixir-dev

### Fixed

- an organization login whose dashboard request is cancelled while profiles are discovered can still be polled and registered; choosing a profile has its own 10-minute window instead of the device code's, a wrong choice keeps the approved login, AWS `slow_down` raises the poll interval to at most 60s, profile discovery presents the Kiro CLI identity, and its errors are translated
- `/v1/responses` keeps tools with the same name in different namespaces apart (#122): each gets a collision-free alias of at most 64 characters, history calls and `tool_choice` use the same mapping, and output items carry the original `name` and `namespace` again, streaming and not. Kiro no longer rejects such requests with `REQUEST_BODY_INVALID`
- a tool declared twice identically is sent once; two different definitions under one name, or a `tool_choice` that matches several namespaces, get a clear 400 before Kiro is called
- duplicate tool names on chat completions and messages get a local 400 naming the tool instead of Kiro's opaque `REQUEST_BODY_INVALID`

## [0.2.11](https://github.com/minpeter/kiro-lb/compare/v0.2.10...v0.2.11) - 2026-11-02

### Added

- request and token limits on API keys, set when creating or editing a key in the dashboard (`requestLimit` and `tokenLimit` on `POST /api/dashboard/keys` and `PATCH /api/dashboard/keys/{id}`; `null` removes a limit). Limits count a key's whole usage and are checked before each generation; a key that has reached one gets the spent-balance answer (402 `billing_error` "Your credit balance is too low" on Anthropic, `insufficient_quota` on OpenAI), so clients stop instead of retrying
- per-key model access: when creating or editing a key, choose all models or a list (`allowedModels` on `POST /api/dashboard/keys` and `PATCH /api/dashboard/keys/{id}`; `null` is every model). A key asking for another model gets 403 (`permission_error` / `model_not_found`) and `/v1/models` lists only its models
- credit cost table: `gpt-5.6-luna` 1.1x → 0.6x (1.2x above 272K), `deepseek-3.2` window 164K, `minimax-m2.5` and `minimax-m2.1` window 196K

### Changed

- requests to Kiro present the captured Kiro CLI 2.37.0 instead of the Kiro IDE 1.1.70: `aws-sdk-rust` user agents, `origin: KIRO_CLI`, the `AmazonCodeWhispererStreamingService.GenerateAssistantResponse` target, `envState`, and no machine id, client attribution, `agentMode`, `agentContinuationId` or `rootConversationId`; token refresh and the browser sign-in exchange send `User-Agent: Kiro-CLI`
- upstream connections use HTTP/1.1 without ALPN, as the CLI does; `UPSTREAM_HTTP2=true` restores HTTP/2 with keepalive pings

### Security

- `/v1` generation routes reject a missing or invalid key before reading the request body, and dashboard routes check the session first and cap bodies at 64 KiB (login) and 8 MiB, so unauthenticated clients can no longer make the gateway buffer 64 MiB per connection
- `/metrics` accepts only `METRICS_TOKEN` or `PROXY_API_KEY`; dashboard-issued keys, which may be handed to other people, no longer see every key's usage
- the dashboard refuses same-site and cross-site browser requests with a password too, and its POST/PUT/PATCH routes require `Content-Type: application/json`, so a forged form post from a sibling subdomain is rejected
- the request log takes the client address from `X-Forwarded-For` only when the connection comes from a proxy in `TRUSTED_PROXIES` (default loopback); an entry that is not an IP or CIDR is reported at startup
- a key's limits and model list fail closed: when they or its usage cannot be read, the request gets a retryable 503 instead of being served unrestricted

### Fixed

- account quota counts Kiro's extra credits: active bonuses (first-upgrade trial credits), purchased add-ons and an active free trial are added to the plan allowance, so an account whose plan reads 978/978 with 462 bonus credits left is no longer shown or routed as out of quota
- automatic history trimming keeps the system prompt; a request it cannot fit fails as too large instead of reaching Kiro without its instructions
- idle HTTP/1.1 upstream connections are retired after 240s, before Kiro closes them (250–400s), so a request is not written to a closing socket
- the API key limit fields reject separator-only input and decimal commas (`1,5m`) instead of reading them as no limit or as 15M
- the system prompt is sent as its own leading message pair instead of being prepended to the first question, so a new conversation with the same system prompt reuses Kiro's prompt cache (~0.07 instead of ~0.25 credits on Sonnet 5.5 and ~0.5s faster to the first token)

## [0.2.10](https://github.com/minpeter/kiro-lb/compare/v0.2.9...v0.2.10) - 2026-10-25

### Added

- *(inferx)* import Builder ID credentials and enforce tool choices

### Fixed

- complete Kimi pricing and branding and add Haiku 5.5
- keep blue-green rollback state alive

## [0.2.9](https://github.com/minpeter/kiro-lb/compare/v0.2.8...v0.2.9) - 2026-10-17

### Added

- add Kimi K3 model support
- classify account failures from automatic TokenHub diagnostics
- browser sign-in for Google and GitHub that keeps earlier social accounts signed in
- add dedicated InferX account and execution control API

### Fixed

- reject unchanged tokens after forced refresh lease timeout
- classify refresh failures before quarantining credentials
- preserve automatic login diagnostics across saves and pauses
- reject unsupported models before upstream generation
- isolate browser login requests across flow transitions
- address review on browser sign-in
- keep bootstrap credentials out of startup logs

### Other

- bound refresh lease contention regression
- Merge main into browser sign-in and rebuild dashboard
- Support InferX tools, inline images and durable metering provenance
- isolate InferX SSE fixture from endpoint rotation

### Added

- browser sign-in for Google and GitHub (#99): the dashboard opens Kiro's sign-in page with PKCE and takes the callback on `localhost:3128`, or from a pasted callback address when kiro-lb cannot listen there. Each account gets its own session, so a second Google/GitHub user no longer signs the first one out. The device code stays available behind "Use a device code instead"

## [0.2.8](https://github.com/minpeter/kiro-lb/compare/v0.2.7...v0.2.8) - 2026-10-03

### Added

- `claude-sonnet-5.5` (1.3x, 1M window, 128k output) with native reasoning effort
- native reasoning effort for `claude-sonnet-5`
- `fastest` endpoint rotation: the order follows the latest latency measurement per region, a challenger leads only with a median more than 15% lower, a failing endpoint backs off with a cooldown that doubles up to 10 minutes, and an optional schedule re-measures only after traffic
- choose which models `/v1/models` lists; a hidden model still answers requests
- "Refresh models" button that re-reads every account's catalog; a new account is re-read 1 and 5 minutes after it is added, and a failed forced read keeps the current catalog
- `DASHBOARD_AUTH=false` opens the dashboard without a password for a loopback-only gateway; `/v1` still requires a key and startup warns
- the request log shows output speed and the reasoning effort actually sent to Kiro
- note on Claude Code's Write, Edit, MultiEdit and NotebookEdit (`CLAUDE_WRITE_HINT`, also in the dashboard) asking the model to save large files in parts
- on the first start after updating, tool shortening, the Write/Edit note, endpoint rotation and the `fastest` order turn on once; a value pinned by an environment variable is kept, and later dashboard changes stick
- the `fastest` order measures every endpoint at startup once an account is ready (3 short generations per endpoint)

### Fixed

- end the turn as `max_tokens` / `length` when Kiro cuts a response inside a tool call, or with its `MODEL_TEMPORARILY_UNAVAILABLE` event after output started, instead of a 500 the client retried forever; the cut call is dropped
- record a response Kiro cut in the request log (warning icon and a detail field), including a cut tool call after complete ones, which ends the turn as `tool_use`
- only `/v1` sends `access-control-allow-origin: *`; with `DASHBOARD_AUTH=false` the dashboard refuses cross-site requests (`Sec-Fetch-Site`, then Origin against Host)
- a failing background account initialization backs off from 10s up to 5 minutes, and a forced token refresh honours the 30s backoff
- an account still initializing no longer makes the pool answer 403, and usage that reports the quota spent with overage off counts as out of quota (402)
- a single-endpoint ping no longer changes routing; a full probe replaces the region's measurements, so models never mix, and the write runs off the async runtime
- tool calls recovered from `[Called …]` text are counted once in output tokens
- a session's input calibration applies only to the model it was measured on, and web-search follow-ups no longer feed it
- the per-account rate chart states its bucket and time window again; the request log filters wrap below the title on small screens
- device login shows the approval link as a fallback when the clipboard is unavailable
- a period change is no longer overwritten by a poll already in flight, and its failures reach the dashboard's error handling
- the light theme's terracotta is darkened to `#b85a36` for WCAG AA contrast on buttons
- the mobile tab bar uses left/right arrow keys; a failed sign-in alert is dismissed once signed in
- the `fastest` endpoint strategy is no longer overridden by the endpoint that last served an account, and a cooldown of 0 disables its backoff
- a transient token refresh failure backs off for 30s even after the access token expired, instead of retrying on every request
- hidden models are stored by Kiro id, so a hyphenated or dotted spelling can be listed again from the dashboard
- a session re-pinned while it was being evicted no longer counts against its old account twice
- "Refresh models" reads every account at once, and a failed forced read no longer postpones the next scheduled one
- answer a pool that cannot serve the model with errors clients do not retry: `not_found_error` 404 when no account has the model, `billing_error` 402 (OpenAI `insufficient_quota` 402, a status its SDKs do not retry) when every account is out of monthly quota, `permission_error` 403 when they are all suspended or signed out
- report the 1M window for Opus 4.7/4.8/5/5.5 and Sonnet 5 instead of 666667, so reported input tokens are no longer a third low
- send the adaptive thinking block with Claude reasoning effort again; without it Sonnet 5 returned no reasoning
- list Claude models in `/v1/models` with hyphenated ids; dotted and hyphenated names resolve to the same model
- keep a session on its account through transient failures (burst limits, busy slots); move it only when the account is suspended, signed out, out of quota or lacks the model
- persist session pins with the runtime state, so a restart or a blue/green switch keeps warm caches; expired pins and pins of a re-bound login are dropped
- honour `session` routing when `ACCOUNT_QUOTA_WEIGHTED_ROUTING=false`
- token refresh: range 60..1800s, default 960s; a transient refresh failure keeps serving the still-valid token and retries after 30s
- return Kiro context overflows as `prompt is too long: N tokens > M maximum` (Anthropic) and `context_length_exceeded` (OpenAI)
- scale the `message_start` input estimate and `count_tokens` by the ratio Kiro reported, so start and end counts agree
- resolve the `auto-kiro` alias for price, window and catalog lookups
- measure time to first token from when the request arrives; Kiro sends headers only once the first token is ready
- count tool calls (name and arguments) as output tokens, so tool-only turns get output and speed
- record no speed when the whole output arrives at once (decode window under 250ms)
- traffic metrics (overview, `/metrics` requests and latency) count only generations, not `count_tokens` or model listing
- the request log model filter joins every spelling of a model (`claude-opus-5-5`, `claude-opus-5-5[1m]`, `claude-opus-5.5`)

### Changed

- dashboard navigation moved to a sidebar grouped by Monitoring, Management and System, with sign-out at its foot; the top bar, the live toggle and the "Refresh usage" button are gone (usage still refreshes every 5 minutes)
- the overview opens with one compact strip of six figures, and its panels use the full width beside the sidebar
- the total request rate chart can show 15 minutes, 1 hour or 6 hours
- the request log lists time, route, model, status and latency; speed, effort, credits and the multiplier moved to the detail view
- settings cards sit in pairs, descriptions are shorter, number fields fit their label, the ping repetitions field is gone, and every save button uses the primary style and is enabled only when its card has changes
- errors, warnings and notices show in one alert stack that fades out on its own (4s notices, 7s warnings, 8s errors) and scrolls into view; gateway error messages, relative times and the rate-limit guide are translated into all four languages
- the light theme is white with Claude's terracotta accent, the dark theme near-black with Kiro's purple accent
- adding an account copies the approval link instead of opening it, shows the time left, and the success notice fades out
- the mobile tab bar stays horizontal under the sidebar layout

### Removed

- the agent task mode option; requests always send `vibe`
- the system prompt condenser; tool description shortening stays and is now on by default

## [0.2.7](https://github.com/minpeter/kiro-lb/compare/v0.2.6...v0.2.7) - 2026-09-30

### Fixed

- give every account its own stable machine id instead of one id shared by all accounts
- present the current Kiro IDE client (1.1.70) in every user agent, without the CLI marker
- send management calls (model list, usage limits) with the same shape and headers as the Kiro IDE
- send Claude reasoning effort without the extra thinking block, as the Kiro IDE does
- keep `agentTaskType: vibe` in spec mode, as the Kiro IDE does
- keep `additionalProperties` in tool schemas

## [0.2.6](https://github.com/minpeter/kiro-lb/compare/v0.2.5...v0.2.6) - 2026-09-30

### Fixed

- stop counting thinking signatures as prompt tokens in the payload guard (#93)
- keep mid-conversation system messages in place so the prompt prefix stays stable and Kiro's prompt cache is reused
- keep idle upstream connections open for 30 minutes with HTTP/2 keepalive pings

## [0.2.5](https://github.com/minpeter/kiro-lb/compare/v0.2.4...v0.2.5) - 2026-09-29

### Fixed

- flag social accounts a new social login signed out
- match Kiro CLI additive credit metering

### Other

- drop stray blank line from static/index.html
- keep LF line endings in static/index.html

## [0.2.4](https://github.com/minpeter/kiro-lb/compare/v0.2.3...v0.2.4) - 2026-09-28

### Fixed

- keep each device-login account's credential separate: login identity no longer derives from the shared profile ARN (#86)
- group banned accounts below paused accounts

### Other

- extract account grouping for direct tests

## [0.2.3](https://github.com/minpeter/kiro-lb/compare/v0.2.2...v0.2.3) - 2026-09-28

### Added

- check releases and install standalone updates from dashboard

### Fixed

- use official model provider marks
- seed dashboard reload detection from served document version
- preserve stop intent and reload dashboards on version changes
- guard failed Windows replacements and recover update targets

### Other

- Move appearance controls into settings
- Merge main and preserve dashboard update review fixes

## [0.2.2](https://github.com/minpeter/kiro-lb/compare/v0.2.1...v0.2.2) - 2026-09-28

### Added

- Support native GPT reasoning effort controls without synthesizing reasoning text.
- Route requests by model capability and available account capacity while retaining suitable session affinity.
- Add reversible Codex CLI and Claude Code setup, diagnosis, status, and restoration commands. Automatic setup and restoration are Linux-only; read-only commands remain cross-platform.

### Fixed

- Keep catalog refresh and failed-account recovery off the request path, with single-flight initialization and bounded warm-up waits.
- Validate authentication and API regions before outbound requests, reject malformed credential imports, and preserve the last-good credential snapshot on source-read failures.
- Meter credits per physical generation on the originating account, handle repeated snapshots without double counting, and distinguish valid non-credit completion markers from malformed metering frames.
- Bind durable authentication and quota state to the current login, discard stale refresh results, and apply the same quota freshness rules to account eligibility and routing weight.
- Preserve concurrent client edits and recovery journals during setup failures, bound diagnostic responses and configuration files, clear competing Claude credentials and provider selectors, and reject unsafe plaintext proxy configurations.
- Avoid exposing account identifiers in capacity errors and prevent late requests from mutating replacement accounts or refreshing their affinity.

### Other

- Use the short-lived GitHub Actions token for draft releases, validating all four binary artifacts and their checksums before publication.

## [0.2.1](https://github.com/minpeter/kiro-lb/compare/v0.2.0...v0.2.1) - 2026-09-27

### Fixed

- *(debug)* sanitize opaque inputs and exported stream content
- *(debug)* address review on sanitizer, failure classification, export
- make model discovery part of activation and readiness
- *(debug)* redact all request text when content capture is off
