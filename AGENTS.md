# PROJECT KNOWLEDGE BASE

**Commit:** 2ce5912
**Branch:** main

## OVERVIEW

`kiro-lb` is a FastAPI reverse-engineered proxy that exposes Kiro (Amazon Q
Developer / CodeWhisperer) through OpenAI and Anthropic compatible APIs, plus a
private React operations dashboard. Python 3.10+, httpx, loguru, tiktoken.

## STRUCTURE

```
kiro-lb-python/
├── main.py                  # App factory, lifespan, router+static mounting
├── kiro/                    # Package: routes, converters, streaming, auth
│   └── static/              # BUILD OUTPUT of frontend/ — never hand-edit
├── frontend/                # Bun + Vite + React 19 dashboard source
├── tests/                   # pytest; network-isolated by conftest fixture
├── docs/                    # Authoritative design refs + translated READMEs
├── data/                    # Runtime credentials/state (gitignored)
├── docker-compose.yml       # Upstream-style deployment
├── docker-compose.homelab.yml  # This operator's live deployment
└── manual_api_test.py       # Manual live-API script, excluded from pytest
```

## WHERE TO LOOK

| Task | Location | Notes |
|---|---|---|
| Add/modify an endpoint | `kiro/routes_openai.py`, `kiro/routes_anthropic.py` | Dashboard routes live in `kiro/dashboard.py` |
| Request -> Kiro payload | `kiro/converters_core.py` | 1397 lines; both adapters delegate here |
| Kiro -> client stream | `kiro/streaming_openai.py`, `kiro/streaming_anthropic.py` | Shared event model in `kiro/streaming_core.py` |
| AWS event-stream framing | `kiro/parsers.py` | Frame reassembly + bracket tool-call recovery |
| Stream order invariants | `kiro/sse_validation.py` | Raises `StreamProtocolError` mid-stream |
| Account failover | `kiro/account_manager.py` | Circuit breaker, sticky routing, lazy init |
| Credentials | `kiro/auth.py` | 4 sources; auto-detects SSO OIDC vs Desktop |
| Model name handling | `kiro/model_resolver.py` | Never raises; unknown names pass through |
| Failure capture/replay | `kiro/debug_capture.py`, `kiro/debug_replay.py` | Redacted by default |
| Payload size repair | `kiro/payload_guards.py` | Kiro rejects ~615KB+ |
| Protocol findings log | `.omo/ulw-research/` | Gitignored research notes |

## CODE MAP

| Symbol | Type | Location | Role |
|---|---|---|---|
| `build_kiro_payload` | function | `kiro/converters_core.py:1224` | Single funnel for every upstream request |
| `parse_kiro_stream` | async gen | `kiro/streaming_core.py:130` | Only place raw frames become `KiroEvent` |
| `KiroEvent` | dataclass | `kiro/streaming_core.py:66` | Protocol-neutral event both adapters consume |
| `AccountManager` | class | `kiro/account_manager.py:179` | Owns pool state, failover, persistence |
| `KiroAuthManager` | class | `kiro/auth.py` | Token lifecycle, region/host resolution |
| `ModelResolver` | class | `kiro/model_resolver.py` | 4-layer normalize -> cache -> hidden -> passthrough |
| `validate_live_openai_payload` | function | `kiro/sse_validation.py:204` | Fails the stream instead of shipping bad order |

## CONVENTIONS

- Reasoning is forwarded only from native upstream frames. Never synthesize it
  from response text or prompt tags (`docs/adr/0001-real-upstream-reasoning-only.md`).
- OpenAI reasoning is emitted as `reasoning` (current vLLM contract), not the
  legacy `reasoning_content`; requests accept both on input.
- Streaming uses a per-request `httpx.AsyncClient`; non-streaming uses the shared
  pooled client. Reusing the shared client for streams leaks CLOSE_WAIT.
- Any new client-visible behavior must land on OpenAI **and** Anthropic, in both
  streaming and non-streaming paths.
- `pytest.ini` and some legacy comments are Russian; new code, comments, and
  docstrings are English only.

## ANTI-PATTERNS (THIS PROJECT)

- Editing `kiro/static/**` by hand — it is `frontend/` build output
  (`frontend/vite.config.ts:31` sets `outDir: "../kiro/static"`).
- Putting images into `userInputMessageContext`; they belong directly in
  `userInputMessage.images` (`kiro/converters_core.py:1177`).
- Client-specific stream mutilation in the gateway. The removed
  `OPENAI_SINGLE_BLOCK_TOOL_COMPAT` mode dropped reasoning, pre-tool text, and
  parallel tool calls; the real defect is in the client adapter.
- Applying the Claude token-correction coefficient to `prompt_tokens`
  (`kiro/streaming_openai.py:351`).
- Rejecting unknown model names. Kiro is the arbiter, not this gateway.
- Trusting `strings`-style assumptions about upstream metadata: Kiro emits only
  `contextUsagePercentage` and a credit `meteringEvent`. No cache-hit fields
  exist, so `cache_read_input_tokens` stays absent until upstream sends it.

## COMMANDS

```bash
python main.py --host 127.0.0.1 --port 8000   # run gateway
pytest -q                                     # full suite (1588 tests)
pytest tests/unit/test_streaming_openai.py -v  # focused protocol suite
cd frontend && bun run build                   # rebuild dashboard into kiro/static
docker compose -f docker-compose.homelab.yml up -d --build
```

## NOTES

- `main.py` refuses to start when `credentials.json` has no usable account, even
  with `ACCOUNT_SYSTEM=false`; set `KIRO_CLI_DB_FILE` for a standalone run.
- The OpenAI `developer` role must be folded into the system prompt; dropping it
  makes Kiro answer `REQUEST_BODY_INVALID`.
- "Improperly formed request" is Kiro's catch-all validation error. Treat it as
  a signal to diff the emitted payload, not as a specific cause.
- 36 Python files exceed 500 lines; `kiro/converters_core.py` and
  `kiro/streaming_anthropic.py` are the highest-risk edit sites.
