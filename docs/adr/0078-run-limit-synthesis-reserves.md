# ADR-0078: Run-limit synthesis reserves

- **Status:** Accepted
- **Date:** 2026-09-01
- **Related:** Sections 6.5 and 12 of the engineering plan; ADR-0023,
  ADR-0059, ADR-0063
- **Detailed designs:** `docs/plan/runtime-loop.md`, `docs/plan/scheduling.md`
- **User authorization:** repair the failed 09:00 scheduled briefing with a
  final-synthesis reserve and a USD 5 run budget

## Context

`RunLimits` carries final-synthesis reserves for steps, model calls, and cost.
The runtime currently interprets those fields only when `Run.kind` is
`delegated`, even though the values are persisted on every run and schedule
revisions copy their complete limits onto materialized runs. A scheduled
research run can therefore spend its last allowed model call or dollars on
another tool-planning turn and fail without returning the answer it already has
enough evidence to write.

Giving scheduled runs a new persisted kind would distinguish their submission
source, but the schedule occurrence and session metadata already carry that
identity. It would also leave the meaning of the reserve fields dependent on an
unrelated discriminator.

## Decision

1. A positive `RunLimits.synthesis_reserve_steps`,
   `synthesis_reserve_model_calls`, or `synthesis_reserve_cost` is an execution
   contract for every run kind. All-zero reserves preserve the previous
   behavior.
2. When any remaining governed dimension reaches its reserve, the runtime adds
   a volatile platform-trusted instruction to the next model request. The
   instruction requires a final answer from evidence already in context and is
   not appended to conversation history or persisted in a checkpoint.
3. A tool call returned while the reserve is active fails closed with
   `SynthesisReserveViolation` and identifies the dimension. Budget accounting
   otherwise remains unchanged: provider usage is still recorded atomically,
   and a single provider call may cross a hard cost limit before it can be
   stopped.
4. Schedule revisions opt in by pinning positive reserve fields in their
   existing `RunLimits`. Materialization continues copying the complete limits
   value and needs no new run kind, column, event, route, or migration.

## Consequences

- Scheduled research can preserve bounded headroom for a final response while
  retaining the ordinary run loop and budget ledger.
- Delegated children keep the same reserve behavior and failure vocabulary.
- A malformed prompt or provider that still requests tools in synthesis-only
  mode fails instead of consuming the protected headroom.
- The reserve is a pre-call control, not a prediction of the next provider
  charge. Operators must size its cost component above the expected final-call
  cost and retain a hard total budget.

## Alternatives considered

- **Add `RunKind.SCHEDULED`:** rejected because occurrence and session metadata
  already identify scheduled provenance, while reserve semantics belong to the
  limits value itself.
- **Automatically reserve a fixed fraction of every run:** rejected because it
  changes existing interactive budgets and makes small run limits unusable.
- **Allow one more tool call inside the reserve:** rejected because the runtime
  could no longer guarantee that the configured headroom remains available for
  synthesis.
