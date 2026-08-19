# ADR-0043: Milestone 7 context-engine seams

- Status: Accepted
- Date: 2026-08-04
- Related: Milestone 7, ADR-0003, ADR-0006, ADR-0007, ADR-0009,
  ADR-0020, ADR-0023, ADR-0024
- Detailed design: `docs/plan/context-engine.md` and
  `docs/plan/runtime-loop.md`

## Context

Milestone 7 is the first implementation of the durable context plan, absolute
budget allocator, deterministic history cut, trust envelopes, structured working
state, and pressure-driven compaction. The detailed designs fix the observable
properties but leave several implementation seams where their example types do
not carry enough information or where two documents describe different ownership
of the same checkpoint write.

The choices below are explicit accepted deviations rather than interpretations.
The context design says the tool set is pinned at session open, but model routing
does not occur at session creation in the implemented application boundary. It
also says compaction is a routed model call, while the engineering plan asks the
first compactor to stay deliberately unsophisticated. Owner review accepted
those choices as visible precedent for this implementation.

## Decisions

1. **A context plan is an idempotent session event.** `context.plan.created` and
   `context.epoch.rotated` carry the complete immutable plan. Reconstructing a
   planner scans the authoritative event log and selects the greatest epoch, so
   no second mutable plan table can disagree with replay. The derivation key is
   `(session_id, epoch)`.
2. **The first plan is pinned immediately before the first routed model request.**
   A bare session row has an agent version but no resolved provider, model limits,
   or provider-specific cache capabilities. Planning after routing supplies those
   values without moving model selection into session creation. This is later than
   the detailed design's literal “session open” language; a future session-open
   routing phase may move it earlier without changing the persisted event shape.
3. **`ContextPlanner.plan` accepts `ResolvedModel`, and `ContextPlan` carries
   pinned `ToolSpec` values.** Capabilities alone do not include the model id,
   context window, or output reserve required by the allocator. Names and a schema
   hash prove identity but cannot reproduce a request after the registry changes,
   so the plan also stores canonical tool specifications. Names and specifications
   must match at validation time.
4. **Prefix size is one conservative, immutable plan-time measurement.** The
   current estimator has no provider tokenizer dependency; it measures the
   canonical prefix once, adds framing overhead, and applies the configured
   safety margin to the body. Reconciliation can change future body estimates but
   cannot rewrite an existing plan's prefix count or hash. This is conservative
   rather than provider-tokenizer-exact and is an owner-review item against the
   detailed design's exact-measurement wording.
5. **In-turn provider chronology outranks the context table's current-user-last
   shorthand.** On the first step, runtime metadata and working state precede the
   current user message. After a tool call, the call and result remain after that
   user message in provider-valid chronological order. Moving the original user
   message behind a later tool result would fabricate a conversation order that
   never happened and can violate provider tool-pair protocols.
6. **The compactor is pure over its inputs; the runtime owns the checkpoint
   write.** The compactor returns an updated checkpoint beside `CompactionResult`.
   `build_with_pressure` adopts it, emits `context.compacted`, and calls the one
   checkpoint writer with the `compaction` trigger. This reconciles the context
   port's result-only example with the runtime design's requirement that the loop
   immediately adopts the new checkpoint, without giving a context component a
   second persistence boundary.
7. **The first compactor is deterministic, structured, and extractive.** It
   summarizes only trusted text, elides untrusted spans into typed provenance
   pointers, unions source event ids, protects all items at or beyond the run's
   seed event, and caps depth at two. It does not make a separate model request.
   This follows the engineering plan's instruction not to build sophisticated
   compaction before the basic loop, but differs from the detailed design's later
   decision to route a versioned compaction prompt through `ModelRouter`. Owner
   review accepted this first implementation without requiring a model-backed
   compactor.
8. **Working-state carry is event-derived.** The control tool and runtime question
   lifecycle emit `context.working_state.updated`. A new run queries the latest
   principal-visible state event below its fixed seed sequence, drops completed
   tasks, and resets `next_action`. Checkpoints remain rebuildable materializations
   rather than the session system of record.
9. **Estimator reconciliation preserves the raw estimate, not a stale corrected
   value.** Raw estimates are memoized by model id, kind, and canonical payload
   hash. Every call applies the current per-model correction factor, so actual
   usage can tune repeated payloads. Two builds are stable while estimator state
   is unchanged; reconciliation occurs only after a model response.

## Consequences

- Context plans, tool advertisement, prefix epochs, and working-state carry
  survive worker and composition reconstruction through the existing event log.
- Authorization revoked after planning does not rewrite the prefix; the run stamps
  current scopes and the ordinary tool policy gate denies the call.
- Active tool pairs and the current user turn never enter compaction input, even
  when old history creates pressure.
- The fifty-turn evaluation can cross midnight, change scopes, add memory events,
  and compact repeatedly while observing one prefix hash.
- Decisions 2, 4, and 7 are accepted, documented differences from literal
  detailed-design language.

## Alternatives considered

- **Store plans in a mutable table:** rejected because the event log would record
  epoch changes while another row independently claimed the current value.
- **Resolve the model during session creation:** rejected for this milestone
  because it moves routing and credential-sensitive provider selection into a
  service that currently owns only agent-version pinning.
- **Re-resolve tool specifications on every build:** rejected because registry or
  scope changes would rewrite the frozen prefix without an epoch.
- **Place every current user message last after tool results:** rejected because it
  changes provider-visible chronology and can orphan tool protocol items.
- **Let the compactor write its repository row directly:** rejected because it
  creates a second checkpoint transaction owner and bypasses the runtime's trigger,
  lease, and rollback rules.
- **Summarize untrusted content into fluent text:** rejected because it launders
  trust and turns inert external data into platform-authored narrative.
