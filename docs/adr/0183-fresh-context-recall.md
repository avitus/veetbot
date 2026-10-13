# ADR-0183: Fresh evidence on context assembly

- Status: Accepted implementation mechanism under ADR-0178; semantic activation remains gated
- Date: 2026-10-11
- Related: ADR-0020, ADR-0178, ADR-0182
- Detailed design: `docs/plan/general-memory-recall.md`, `docs/plan/context-engine.md`

## Problem

The context builder retained rendered recall by run, step and checkpoint identity.
Those identify the question, not whether its evidence is still current. A completed
sparse result could hide a subsequently prepared semantic match. A completed dense
result could survive correction, erasure, elapsed source or lease expiry, and
withdrawn admission without ever reaching the retriever's current-source checks.
The production People/memory watermark also missed elapsed expiry and changes in
optional-channel availability. This affects any subject or retrieval category.

## Decision

Production context assembly reads current permitted evidence every time. It retains
no completed recall bundle between assemblies. The runtime already obtains pressure
and the model request from one atomic `assemble` invocation, so this does not add a
second retrieval to the normal model step. A compaction retry reads again against
its changed checkpoint. No embedding/model call, preparation job or new reservation
is added. The bounded query-vector cache remains independent of rendered evidence.

The existing `recall_revision` hook becomes explicit opt-in to completed-result
reuse. Its caller must certify every mutable evidence dependency, including time,
visibility, policy and channel availability; a memory or People watermark alone
cannot do so. Production supplies no such hook. An absent hook, `None` result or
failed revision read discards this run's retained bundle and performs fresh recall.
Revision failures log only the run identifier and exception class. Fresh reads are
awaited directly, so cancellation stops them without detached recall work. A restored
revision cannot revive an entry discarded while validation was unavailable.

Opted-in cache identity also includes the full principal, run and checkpoint,
besides the active query context and frozen snapshot identity. Its existing bound
and revision invalidation remain. The immutable fixture used by the legacy separate
`measure`/`build` test explicitly supplies a fixed revision; its one-trace assertion
is unchanged. The runtime gate continues to require exactly one atomic assembly
per model request. A caller without a complete revision pays for fresh recall on
separate `measure` and `build` calls and should use `assemble` when it needs both.

New evidence and correction lines remain in Region B. The frozen prefix and snapshot
are unchanged. Existing dependency/erasure fences still govern provider egress;
this change does not claim to eliminate the interval between a read and egress.
No new provider or semantic channel is activated, and no M33 gate is marked complete.

## Verification and limits

Six behavioral regressions fail against the previous builder: newly prepared
semantic evidence stays absent, and previously rendered evidence survives source
correction, erasure, expiry, admission withdrawal or lease expiry. Further tests
cover ordinary project, preference and routine recall, production composition with
People both enabled and disabled, unavailable revision recovery, authority/checkpoint
changes, cancellation and transient read failure. PostgreSQL exercises a sparse
context followed by prepared recall and a source correction in the same checkpoint.
Prefix identity and provider-call counts remain unchanged in these checks.

The tradeoff is additional local reads for repeated assemblies of an identical
checkpoint. Production-shaped latency must still be measured for M33 activation;
these tests establish freshness, not answer quality or release performance.
Production semantic admission/authority, query/index scheduling, historical/People
semantic parity, task-aware planning, recovery and held-out formation/answer-use
evidence remain open.
