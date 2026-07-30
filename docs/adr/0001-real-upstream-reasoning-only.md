# ADR 0001: never synthesize reasoning

## Status

Accepted — 2026-07-29.

## Context

The upstream `kiro-gateway` fork implemented “extended thinking” by injecting
XML-like instructions into a user prompt and parsing model-generated text back
into OpenAI `reasoning` or Anthropic thinking blocks. That is not an
upstream Kiro protocol capability and misrepresents ordinary output as verified
model reasoning.

## Decision

`kiro-lb` removes the tag injection and response parser. It accepts client
compatibility fields such as OpenAI `reasoning_effort` and Anthropic `thinking`
without failing the request, but does not translate them into prompt text,
upstream payload changes, or synthetic response fields.

The proxy emits reasoning blocks only from a captured Kiro upstream wire
protocol. On 2026-07-29, a live runtime probe verified that Claude Opus 4.6,
Opus 4.7, and Sonnet 4.6 return structured `{ "text": ... }` adaptive
reasoning frames when a command-level `additionalModelRequestFields` payload
contains `thinking: { type: "adaptive", display: "summarized" }` and
`output_config: { effort: "max" }`. The proxy maps only those upstream frames
to OpenAI `reasoning`; it never extracts reasoning from normal content.

## Consequences

- Existing clients remain compatible, but their reasoning panels can be empty.
- Prompt token usage and responses are no longer altered by the proxy.
- Any additional native model or effort level must be independently verified.
- Regression tests: `tests/unit/test_real_only_reasoning.py` and
  `tests/unit/test_native_reasoning.py`.
