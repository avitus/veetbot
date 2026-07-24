# ADR-0007: Provider-neutral reasoning state

- Status: Accepted
- Date: 2026-07-17
- Supersedes / amends: ADR-0006 (no private reasoning storage)
- Related: ADR-0002 (provider-neutral model protocol)

## Context

Version 2.0 makes the OpenAI Responses adapter and the Anthropic Messages
adapter co-equal first real adapters. Both target reasoning-capable models,
and reasoning is where provider-neutrality is hardest:

- Reasoning tokens are **billed** (Anthropic bills thinking within
  `output_tokens`; OpenAI reports `output_tokens_details.reasoning_tokens`), so
  usage and cost accounting that ignores them is wrong.
- During an **active tool loop**, providers require prior reasoning to be
  returned **verbatim**:
  - Anthropic requires the unmodified `thinking` / `redacted_thinking` blocks,
    including their opaque `signature`, in the last assistant message, or it
    rejects the request with `400 invalid_request_error` ("thinking blocks ...
    cannot be modified").
  - OpenAI benefits measurably (~3% on SWE-bench in its own cookbook) from
    including reasoning items, and supports `reasoning.encrypted_content` for
    stateless / ZDR operation with `store=False`.
- This collides with ADR-0006, which says private reasoning is never stored.
- Provider reasoning state is **opaque and non-portable**: provider A's
  reasoning payload is meaningless to (and rejected by) provider B.

## Decision

1. Normalize reasoning as two separate things: a transient `ReasoningDeltaEvent`
   for display, and a `ProviderReasoningItem` (opaque, provider-tagged) for
   continuity. Raw reasoning text is display-only.
2. Carry the opaque continuation (`ProviderContinuation` / `ProviderReasoningItem`)
   inside the **run checkpoint** for the life of the active tool loop only.
   Never write raw reasoning to the event log, structured logs, or long-term
   memory.
3. **Pin a run to one provider.** Routing is decided once at run start.
   Provider-opaque items are dropped when a run is routed to a different
   provider; only portable items (system, user, assistant text, tool calls and
   results, compacted summaries) cross run/provider boundaries.
4. Account reasoning tokens as billed output in `RunUsage` and cost.
5. `ModelRequest` carries cache hints; adapters translate them (Anthropic
   explicit `cache_control` breakpoints; OpenAI automatic prefix caching).
6. This **amends ADR-0006**: reasoning is never *durably* stored, but opaque
   continuation may live transiently in the checkpoint for one tool loop.

## Consequences

- Preserves both provider-neutrality and the correctness of multi-step tool
  loops on reasoning models.
- Preserves the privacy/safety intent of ADR-0006: no durable chain-of-thought.
- Adds a `provider_continuation` field to the checkpoint and provider tagging to
  opaque items; adds handling to strip them on provider switch.
- Forbids switching providers mid-run; cross-provider continuity within a
  session is limited to portable items.

## Alternatives considered

- **Rely on provider-managed state** (`previous_response_id`): rejected; couples
  to one provider and violates "do not depend exclusively on provider-managed
  conversation state."
- **Store reasoning durably** for simplicity: rejected on privacy/safety grounds
  (ADR-0006).
- **Drop reasoning between model calls**: rejected; Anthropic rejects modified
  block sequences during tool use, and OpenAI loses measurable quality.
