# ADR-0006: No private reasoning storage

- Status: Accepted (amended by ADR-0007, 2026-07-20)
- Date: 2026-07-25 (record written; decision predates it and is cited from
  Section 6.8 and Section 11.4 of version 1.0 of the plan)
- Related: Sections 6.8 (event envelope), 6.9 (checkpoint), 10.6 (reasoning and
  provider continuity), 11.4 (compaction), 19 (observability), 22 (security
  baseline), 31 (trajectory capture and export), ADR-0007 (provider-neutral
  reasoning state), ADR-0012 (open and self-hosted models),
  ADR-0016 (trajectory capture and export)

## Context

Section 4's repository tree names this ADR, and four places in the plan cite it
as settled: Section 6.8 requires that raw reasoning text never be persisted,
Section 11.4 forbids requesting or storing private chain-of-thought, Section
10.6 repeats the prohibition for the event log, structured logs, and long-term
memory, and Section 31 excludes reasoning from trajectory export. The record
itself was never written, so the reasoning behind a constraint that four
sections depend on existed only as those sections' restatements of it.

Writing it now matters more than usual because the decision has already been
amended once. ADR-0007 established that both first providers require some
provider-opaque reasoning state to be returned verbatim during an active tool
loop — Anthropic rejects a request whose last assistant message has had its
signed thinking blocks altered or removed, and OpenAI's Responses API benefits
measurably from replayed reasoning items and supports
`reasoning.encrypted_content` for stateless operation. A flat "never store
reasoning" would have made both first adapters non-functional for multi-step
tool use. The amendment is therefore not a weakening of the original decision
but a clarification of what "store" meant, and without a written record the
distinction between the two lives only in a sentence at the end of Section 11.4.

There are three independent reasons the original decision holds, and they are
worth separating because they fail differently.

The first is contractual. Provider terms and the models' own training treat
reasoning as scratch, not as a statement the provider stands behind. Persisting
it and later surfacing it — in an audit, an export, a support ticket — presents
an unendorsed draft as a record of what the system decided.

The second is that reasoning text is the least filtered thing a model produces.
It restates its input, which means it restates whatever untrusted content the
input contained, including secrets that a redactor caught on the way out but
that reappear paraphrased. A durable store of reasoning is a durable store of
everything the model was ever shown, in a form no redaction pass was designed
for.

The third is that reasoning is the wrong artifact to reason from. Sections 6.8
and 11.4 already require persisting messages, actions, evidence, concise
decision summaries, and structured working state — the things that are actually
load-bearing for replay, memory formation, and audit. Reasoning text correlates
with those but does not determine them, and a system that debugs from reasoning
text will eventually treat a plausible narrative as the cause of a behaviour it
did not cause.

## Decision

1. **Raw reasoning text is never persisted** to the event log, to structured
   logs, to projections, to long-term memory, or to trajectory exports. This is
   the original decision and it is unchanged.

2. **Private chain-of-thought is never requested as a product feature.** Where a
   provider exposes reasoning, it is consumed as a transient display signal and
   as opaque continuation state, not as content to retain.

3. **Reasoning is a transient transport event.** `ReasoningDeltaEvent` streams
   to a connected client for display and is not written to the log. Section
   6.8's rule that token deltas are not persisted by default covers reasoning
   deltas for the same reason.

4. **What is persisted about reasoning is that it happened and what it cost.**
   The event log records the occurrence and the reasoning-token count; Section
   10.6 requires those tokens to be accounted as billed output in usage and
   cost. An operator can therefore see and bill for reasoning without a stored
   transcript of it.

5. **Amendment (ADR-0007, 2026-07-20): provider-opaque continuation may live in
   the run checkpoint for the life of a tool loop.** "Do not store" refers to
   durable logs, the event payload, and long-term memory. It does not forbid
   holding a provider's opaque reasoning-continuation payload — Anthropic
   thinking blocks with their signatures, OpenAI reasoning items or
   `reasoning.encrypted_content`, Gemini thought signatures — inside the active
   `RunCheckpoint` for the duration of a tool loop. That payload is
   provider-tagged, replayed verbatim, excluded from logs and memory, and
   discarded when the loop ends or the provider changes. A run is pinned to one
   provider and reasoning state is never ported across providers.

6. **In-band reasoning is scrubbed, not stored.** Open and self-hosted models
   emit reasoning as `<think>` text in the response body (Section 10.6,
   ADR-0012). The boundary-gated streaming scrubber removes it from the user
   surface and from anything persisted; it is not an exception to this ADR
   because the transport differs.

7. **Trajectory export excludes reasoning.** Section 31 lists raw reasoning
   among the excluded categories alongside secrets and policy-restricted PII.
   An export is a durable artifact leaving the system, which is the case this
   ADR most directly governs.

## Consequences

- Debugging a run means reading its events, its tool calls, its evidence, and
  its decision summaries. This is harder than reading a reasoning transcript
  would be, and the discipline it forces — that anything needed for diagnosis
  must be recorded as a decision summary or structured working state — is a
  design constraint on every subsystem, not only on the gateway.
- Multi-step tool use works with both first providers, because the amendment
  permits the continuation payload the providers require.
- The checkpoint carries provider-opaque bytes that cannot be reconstructed from
  the event log, which is why the event-log spec keeps opaque items inline in
  checkpoints while storing conversation as event references.
- Losing a checkpoint mid-loop loses the continuation payload, so the loop
  restarts rather than resumes. That is a deliberate cost of not persisting the
  payload durably elsewhere.
- Memory formation cannot draw on reasoning text. It draws on messages,
  actions, evidence, and outcomes, which is the correct input anyway.
- A future provider that only exposes reasoning as durable server-side state
  would need its own amendment, recorded the same way this one was.

## Alternatives considered

- **Persisting reasoning for debugging only, behind a retention window**:
  rejected; a retention window is an operational control on a store that still
  exists, and every one of the three reasons above applies to a store that
  exists for thirty days.
- **Persisting reasoning with redaction applied**: rejected; reasoning
  paraphrases its input, and redaction is pattern-based. A secret restated in
  prose does not match the pattern that caught it in the tool output.
- **Storing a hash of the reasoning for reproducibility checking**: rejected as
  not useful enough to justify the surface — reasoning is not deterministic
  across calls even at temperature zero, so the hash would differ on every
  replay and prove nothing.
- **Refusing provider-opaque continuation entirely (the unamended reading)**:
  rejected by ADR-0007; it makes both first adapters unable to complete a
  multi-step tool loop, which is the platform's core behaviour.
- **Persisting the opaque continuation payload durably so a lost checkpoint can
  resume mid-loop**: rejected; the payload is reasoning in an encrypted or
  signed wrapper, and durably storing it is durably storing reasoning with an
  extra step. Restarting the loop is the accepted cost.
- **Treating in-band `<think>` text as ordinary output because it arrives in the
  message body**: rejected; the prohibition is about what the content is, not
  about which field carried it.
