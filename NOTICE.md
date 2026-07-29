# Notice

`kiro-lb` is a modified version of
[Kiro Gateway](https://github.com/jwadow/kiro-gateway),
Copyright (C) 2025 Jwadow, distributed under the
GNU Affero General Public License v3.0.

Modifications are Copyright (C) 2026 minpeter and are released under the same
license. The full license text is in [LICENSE](LICENSE).

Because this program is normally run as a network service, AGPL-3.0 section 13
applies: users interacting with a deployed instance are entitled to the
corresponding source of the running version. It is published at
<https://github.com/minpeter/kiro-lb>.

## Significant changes from upstream

### Removed: prompt-injected "fake" reasoning

Upstream produced extended thinking by injecting `<thinking_mode>` tags into the
user prompt and parsing `<thinking>` blocks back out of ordinary response text.
That path is deleted. Reasoning is forwarded only from structured Kiro adaptive
thinking events. See [docs/adr/0001-real-upstream-reasoning-only.md](docs/adr/0001-real-upstream-reasoning-only.md).

### Removed: gateway-authored conversation text

The gateway no longer writes text into the conversation it proxies:

- Synthetic turns (leading assistant, consecutive users, assistant prefill) are
  sent with empty content instead of a readable placeholder.
- Truncation is no longer narrated back to the model as a synthetic
  `[System Notice]` user turn or an `[API Limitation]` tool-result prefix, and
  the system section explaining those markers was dropped. A cut-off turn is
  reported through `finish_reason` / `stop_reason` instead.

### Fixed: stream fidelity

- Content deltas are forwarded verbatim. Upstream sends incremental deltas, so
  suppressing a repeated frame as a "replay" corrupted output such as
  `6666666666` → `6666`.
- The upstream `stopReason` metadata frame is parsed and mapped to each client
  protocol rather than inferred locally.

### Fixed: upstream protocol compliance

- Native adaptive thinking is built from an allowlist. `thinking.type` accepts
  only `adaptive`/`disabled`, and numeric budgets require a minimum of 1024, so
  legacy Anthropic budget requests are translated instead of forwarded.
- Anthropic requests that inline a `role: "system"` turn are accepted and the
  text is hoisted into the system prompt.
- Advertised model aliases (such as `auto-kiro`) are resolved to real upstream
  identifiers before the request leaves the gateway.

### Added: operations dashboard and control plane

- A password-protected dashboard with account health, live subscription quota,
  API-key management, and metadata-only request logs.
- Account registration from the dashboard, writing to the credential store only.
- Multiple hashed data-plane API keys alongside the legacy `PROXY_API_KEY`.
- 429 responses return immediately so the account pool can rotate instead of
  sleeping through per-account retries.

The dashboard frontend additionally vendors and modifies MIT-licensed source
from [Soju06/claude-lb](https://github.com/Soju06/claude-lb); see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Research sources

Upstream behavior was verified with live probes against
`runtime.{region}.kiro.dev`. Findings and the resulting policies are recorded in
[docs/router-design.md](docs/router-design.md). Operational patterns were also
compared against [decolua/9router](https://github.com/decolua/9router) and
[Quorinex/Kiro-Go](https://github.com/Quorinex/Kiro-Go); no code was copied from
either project.
