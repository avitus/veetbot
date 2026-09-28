# ADR-0139: Completed active history precedes fresh step context

- Status: Proposed (owner-requested cache repair, 2026-09-28)
- Date: 2026-09-28
- Amends: ADR-0020's interpretation of the current user message during tool loops
- Related: ADR-0007, ADR-0135, ADR-0137; engineering plan Sections 10.1 and 11
- Detailed design: `docs/plan/context-engine.md`, `docs/plan/model-gateway.md`

## Evidence

The stable tool-output admission repair was deployed and bounded large tool
results correctly. Cache reads nevertheless plateaued within runs, including
a 66-call run with no context-pressure events. Its cache read stayed at 4,340
tokens while input grew to 53,196. A later 24-call run had 24 distinct recall
hashes, one prefix hash, one epoch, and no pressure events. No private message
or tool output is retained in this record.

The builder placed fresh working state, skills, runtime metadata, recall and
corrections before the active run's original user message and every subsequent
tool exchange. Recomputing recall changes its timestamp even when selected
facts are unchanged. That early difference prevents reuse of all later tool
history. The old run marker could therefore describe a prefix that the next
step did not actually repeat. The earlier probes used large carried history,
which masked the failure of the much smaller active suffix to become reusable.

## Decision

Completed active exchanges join retained conversation before fresh context.
An unanswered trailing user input stays after fresh context; when the model
has called tools, that original user message is part of completed history.
Keep complete tool-call/result batches and the latest opaque continuation in
their existing order. Refresh context after them, never inside a tool batch.
The history-window run marker ends before either fresh context or the latest
opaque continuation, whichever comes first. A first request with no earlier
conversation has no history marker; Region A still has its prefix markers.

This refines the context design's assembly order without changing the plan's
Region A/B boundary, trust, authority, budget or provider-neutral requirements.
The builder stays pure and never writes rendered context to checkpoints.
Recall, corrections, working state and skill bodies remain fresh and retain
their trust. The original current input, corrections, state and active pairs
still never yield. Existing class caps, history selection and compaction apply.
Opaque reasoning remains latest-turn-only, checkpoint-only, and provider-pinned.
There is no schema migration, dependency, provider-managed state or new retention.

Existing plans need no epoch rotation: Region A is unchanged. The first request
using the new body order can miss the former body's cache. Within-run cache
reuse still ends before replaced opaque reasoning; it cannot include that
latest exchange until a later request has rendered its portable form. Fresh
context and a new unanswered input are not promised a reusable cache entry.
Provider expiry, routing and genuine history pressure can still cause misses.

## Verification

The red regression uses the real planner, budgeted builder and Responses
translator with large canonical results and bounded persisted context excerpts.
It failed because changing working state rewrote the wire prefix before active
history. Cases cover carried and fresh sessions, with and without opaque
reasoning, and changes to recall, state, date and skill bodies independently.
They require a growing reusable prefix through actual tool results, fresh
context on every step, canonical-source preservation and deterministic
checkpoint replay. Additional cases protect corrections under recall pressure
and place supplemental user input after fresh context without rewriting prior
exchanges. Existing context, provider, artifact and runtime suites remain gates.

Earlier tests that compared an entire first request including its runtime row
and unanswered input now compare every carried tool exchange explicitly; those
fresh rows are intentionally outside the reusable boundary. A test that expected
the next run to differ before the run marker now requires equality, since active
portable history no longer follows the previous run's volatile context.

## Live dev comparison

The same eight-call local harness used the real OpenAI Responses adapter and
Astra with a unique session identity per variant, changing recall timestamps,
and successive genuine engineering-reference sections. Each section was 18,000
characters canonically, with the normal 4,096-byte excerpt admission. The only
variant was the old versus repaired builder. No production messages or private
content were used. The harness forced one read-only fixture tool per call;
the provider returned no opaque reasoning in this sample. The offline matrix
separately checks the latest-turn continuation path.

| Call | Old input / cached | Repaired input / cached | Cached / prior input |
| --- | --- | --- | --- |
| 1 | 381 / 0 | 381 / 0 | — |
| 2 | 1,258 / 0 | 1,259 / 0 | 0% |
| 3 | 2,096 / 0 | 2,093 / 0 | 0% |
| 4 | 2,949 / 0 | 2,946 / 1,847 | 88.2% |
| 5 | 3,831 / 0 | 3,837 / 2,701 | 91.7% |
| 6 | 4,617 / 0 | 4,617 / 3,588 | 93.5% |
| 7 | 5,537 / 0 | 5,531 / 4,370 | 94.7% |
| 8 | 6,434 / 0 | 6,430 / 5,286 | 95.6% |

The cold start and short initial input did not hit. Subsequent reads grew with
the active history instead of remaining at an initial fixed prefix. Estimated
cost at the probe's configured prices, including writes, was USD 0.348775 before
and USD 0.1440545 after, a 58.7% reduction in this sample. The runs took 18.59
and 17.36 seconds respectively.
This demonstrates the repair on a live dev workload, not a guaranteed cache-hit
rate or production confirmation. Deployment and a qualifying new production
run remain the release follow-up.
