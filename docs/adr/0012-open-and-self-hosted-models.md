# ADR-0012: Open and self-hosted model support

- Status: Accepted
- Date: 2026-07-20
- Related: ADR-0002 (provider-neutral model protocol), ADR-0007 (reasoning state), Sections 2.3, 6.5, 10.1, 10.5, 10.6, 10.7

## Context

The model layer currently targets only OpenAI Responses and Anthropic (hosted,
closed). A general-purpose agent needs open-weights and self-hosted models for
cost control, data residency, offline/air-gapped operation, fine-tuning, and to
avoid vendor lock-in. Nous Research's Hermes Agent demonstrates a production
model layer spanning five API modes and thousands of models behind one
normalized transport (`convert_messages / convert_tools / build_kwargs /
normalize_response` + a `provider_data` quirk bag) — including vLLM, Ollama, LM
Studio, LiteLLM, and OpenRouter — with declarative provider plugins, aliases,
credential pools, and a cost-source precedence.

## Decision

1. Add an **OpenAI-compatible `chat_completions` adapter** as a co-equal
   first-class mode alongside Responses and Anthropic. It covers vLLM, Ollama,
   LM Studio, LiteLLM, OpenRouter, and any BYO OpenAI-compatible endpoint,
   including self-hosted Hermes/Llama/Qwen.
2. Model providers are **declarative plugins** (profiles) resolved by the
   registry and user-overridable without editing core: aliases, credential pools
   with round-robin/failover and cooldowns, OAuth, and a model catalog for
   capabilities/limits/pricing.
3. Handle **in-band `<think>` reasoning** (open models emit reasoning as plain
   text) via a boundary-gated streaming scrubber, and maintain a **per-provider
   reasoning-handling matrix** (preserve-signed / replay-encrypted /
   store-signature / in-band-scrub / strip-on-400) — Section 10.6.
4. Provide an **XML `<tool_call>` parser** for models without native function
   calling; use provider-native function calling when available.
5. Extend usage/cost with a **cost-source precedence** (provider cost API >
   generation usage > model catalog > docs snapshot > config override) and
   **additive usage** across subagent fan-out (Section 6.5).

## Consequences

- Broad model reach, real cost/residency control, and the strongest possible
  test of provider-neutrality.
- More adapters and provider quirk-matrices to maintain.
- The normalized protocol (ADR-0002/0007) must accommodate a third reasoning
  representation (in-band text) and non-native tool calling. Provider pinning
  per run (ADR-0007) still holds.

## Alternatives considered

- **Stay OpenAI + Anthropic only**: rejected; not general-purpose, and locks in.
- **Adopt a third-party gateway (e.g. LiteLLM) wholesale**: viable as one
  adapter behind our port, but the normalized protocol and policy stay ours.
