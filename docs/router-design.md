# kiro-lb router design

`kiro-lb` is deliberately a Kiro-only router. It borrows operational patterns
from [9router](https://github.com/decolua/9router) and
[Kiro-Go](https://github.com/Quorinex/Kiro-Go), without copying their code.

## Routing invariants

1. **Preserve the Kiro data plane.** Use the authenticated runtime endpoint
   selected by Kiro credentials. Do not probe unrelated legacy endpoints before
   a live request.
2. **Fail over accounts before waiting.** A 429 is account-specific in a pool,
   so the HTTP client returns it immediately to the account manager instead of
   spending exponential-backoff time on a known-rate-limited account.
3. **Do not fail over malformed requests.** Context overflow, invalid tool
   payloads, and other request-invalid errors remain client errors.
4. **Never fabricate reasoning.** Native adaptive reasoning is enabled only
   for validated models and only when the client asks for an effort level.
5. **Separate control and data planes.** Dashboard sessions cannot call `/v1`;
   data-plane API keys cannot call `/api/dashboard`.

## Account states

| Event | Router action |
| --- | --- |
| 429 rate limit | Immediate rotation to another account; account manager cooldown/backoff |
| 402 quota / overage | Rotate; retain error state for operator visibility |
| 403 | Refresh once in the HTTP client, then rotate if still rejected |
| Invalid model for account | Rotate because tiers can differ by account |
| Context overflow / malformed payload | Return directly; another account will not fix it |
| Network timeout / 5xx | Retry according to network policy, preserving endpoint affinity |

## Native adaptive reasoning

The request path for verified Claude models is:

```json
{
  "additionalModelRequestFields": {
    "thinking": { "type": "adaptive", "display": "summarized" },
    "output_config": { "effort": "max" }
  }
}
```

Upstream constraints confirmed by live probes against
`runtime.us-east-1.kiro.dev`:

| Observation | Consequence |
| --- | --- |
| `thinking.type` accepts only `adaptive` or `disabled` | Legacy Anthropic `{"type": "enabled", "budget_tokens": N}` is translated to an effort level, never forwarded |
| Numeric budget fields require a minimum of `1024` | Smaller budgets are ignored instead of failing the request |
| Unknown members of `additionalModelRequestFields` are rejected | The object is built from an allowlist and omitted when reasoning is not requested |
| `max_tokens` is not accepted in any payload location | Client output limits cannot be enforced upstream |

Sending an invalid object fails the entire request with
`REQUEST_BODY_INVALID`, which is why the field set is never passed through
verbatim from client input.

The parser treats Kiro's separate `{ "text": ... }` adaptive frames as native
reasoning and serializes them as OpenAI `reasoning_content` or Anthropic
thinking blocks. Normal `{ "content": ... }` frames remain final-answer
content. No XML prompt tags or text heuristics are used.

## Stream fidelity

Two upstream signals must be handled exactly:

1. **Content deltas are incremental, never cumulative.** Frames are forwarded
   verbatim. Comparing a frame against the previous one to suppress "replays"
   silently corrupts repeating output, turning `6666666666` into `6666` and
   `1833` into `183`.
2. **The stop reason comes from a metadata frame** such as
   `{"stopReason":"END_TURN"}`. It is mapped to the client protocol so a
   truncated turn is not reported as a clean finish. Unrecognized values fall
   back to local inference rather than inventing a reason.

## Endpoint policy

Other Kiro proxies rotate through several hosts on failure. Measured against
this account, the alternates are not usable and only add latency:

| Host | Result |
| --- | --- |
| `runtime.{region}.kiro.dev` | 200 in ~2s |
| `codewhisperer.{region}.amazonaws.com` | fails after ~4.5s |
| `q.{region}.amazonaws.com` (generate) | fails after ~4.3s |
| `q.{region}.amazonaws.com` (`getUsageLimits`) | 200 — used for quota only |
| `ListAvailableModels` (either host) | 403 not authorized |

So the router keeps endpoint affinity: it sends generation requests only to the
host resolved from the credentials and never probes alternates mid-request.
Rotating hosts would add seconds per attempt without granting fresh quota,
which is the reported cause of multi-second latency in proxies that do it.
Because the live model catalog is unavailable, the advertised list is static
and verified by request rather than discovery.

## Model aliases

`/v1/models` advertises `auto-kiro` so it does not collide with Cursor's own
"auto" entry. Aliases are resolved to the real identifier before the request
leaves the gateway; forwarding the alias verbatim returns `INVALID_MODEL_ID`.

## Payload minimalism

The request body carries only fields this endpoint accepts. Verified by live
probe against `generateAssistantResponse`:

| Field | Result |
| --- | --- |
| `systemPrompt` (top level) | **400** `REQUEST_BODY_INVALID` |
| `conversationState.agentContinuationId` | **400** `REQUEST_BODY_INVALID` |
| `agentMode`, `agentTaskType`, `inferenceConfig` | accepted but inert |
| `max_tokens` in any location | not supported; client limits cannot be enforced |

The system prompt therefore travels inside the first user turn, and no agent
session fields are sent. Adding an unsupported field fails the whole request,
so the payload is never expanded speculatively.

## No injected prompt text

The gateway never adds text the model can read as conversation:

* Synthetic turns (leading assistant, consecutive users, assistant prefill)
  carry empty content. Upstream accepts empty content in every position, and a
  literal placeholder is read as a real user instruction: with
  `(empty placeholder)` an assistant-prefill request answered "Looks like your
  message came through empty" instead of continuing the partial sentence.
* Truncation is never narrated back into the conversation. Earlier revisions
  synthesized a `[System Notice]` user turn and rewrote tool results with an
  `[API Limitation]` prefix on the following request. That text was written by
  the gateway, not Kiro, so it has been removed: a cut-off turn is reported
  through `finish_reason`/`stop_reason` and left for the client to handle.

## Client compatibility

| Client behavior | Handling |
| --- | --- |
| `role: "system"` inline in Anthropic `messages` | Hoisted into the system prompt, because Kiro rejects unknown roles |
| Anthropic `system` as cache-control text blocks | Text extracted; cache markers dropped |
| `output_config.effort` (Claude Code on Opus 4.7+) | Mapped to native adaptive thinking |

## Usage telemetry

Credit usage is fractional, so the dashboard reads the `*WithPrecision`
fields; the integer fields round `720.81` down to `720`. Overage status and
rate are surfaced alongside usage so an account nearing its cap is visible
before requests start failing.

## Data-plane keys

- `PROXY_API_KEY` remains a legacy bootstrap key.
- Dashboard-created `klb_...` keys are scrypt-hashed with a unique salt.
- The raw key is returned exactly once at creation.
- Revocation is immediate; historical prompts and completions are never stored.
