# ADR-0207: Bounded confirmation concurrency

- Status: Accepted for implementation under the authorized M33 recovery; verification pending
- Date: 2026-10-15
- Related: ADR-0178, ADR-0194, ADR-0199
- Scope: Synthetic M33 confirmation orchestration only

## Context

The retained partial confirmation spends substantially more time rebuilding
synthetic histories, distractors and native indexes than answering tasks. The
owner requested faster evaluation without changing the gates or USD 300 aggregate
cap. A zero-external-provider probe on the original source measured 1.68 times
serial throughput with two isolated workers. Four workers reached only 1.87 times
serial throughput and increased contention. These probes are operational
diagnostics, not release-quality evidence or a guaranteed completion time.

## Decision

Keep serial execution as the default. An explicitly named second disposable
database permits at most two concurrent scenario workers in one coordinator
process. Both databases must pass the existing scratch-name and authorization
guards, be distinct, and produce identical schema and actual runtime captures
before held-out tasks are opened or live providers are constructed. Database
credentials do not enter the recorded options or execution manifest.

A version-two execution manifest binds the two-worker choice to the original
measurement identity. Version-one manifests retain their original serialization
and remain serial; old observations cannot acquire a new concurrency setting.
All original input, schema and runtime checks remain in force.

Assign scenarios by their original corpus position modulo the worker count.
Each worker runs all twelve variants of its assigned scenario in the original
order, with that scenario's exclusive formation bank. Every arm still resets its
scratch state, reruns formation, verifies the bank state, indexes through the
normal indexing path and executes the actual reader. Each worker owns its native
encoder instance. No observation, distractor, family, split or measured stage is
removed or replaced by cached results.

One lock-protected provider ledger and allowance cover both workers. Reserve each
in-flight request before sending it. On a worker failure, stop admitting new
provider calls and variants, let already admitted calls settle where possible,
retain every completed row and uncertain reservation, then mark the collection
failed. The whole-job deadline still cancels outstanding work and retains unknown
cost. A failed attempt is never resumed or overwritten.

The reader configuration, per-run search limits, per-answer latency and cost
ceilings, full 4,800-observation matrix, human review and every release threshold
remain unchanged. Measured contention is part of observed latency, not subtracted
from it. Final source review and CI precede any new paid attempt. The coordinator's
run allowance must fit within the owner's remaining aggregate after all earlier
spending and held reservations.

## Required verification

- Reject a duplicate, ordinary, unauthorized or differently configured worker
  database before provider construction or holdout parsing.
- Preserve legacy manifest bytes; bind new worker count and reject unknown or
  contradictory manifest settings.
- Exercise concurrent distinct databases with the same synthetic principal and
  prove isolation, complete scenario/variant coverage and exclusive formation banks.
- Prove one shared cap under simultaneous reservations and out-of-order settlement;
  preserve failures and drain admitted requests without admitting later work.
- Retain independent timing for setup, indexing and runtime. Scripted timing
  probes do not replace live quality, latency or native-load gates.

## Implementation verification

Focused unit checks cover legacy serialization, concurrency admission before
held-out task parsing, complete ordered variants, shared reservations,
out-of-order settlement and preservation of a peer's completed row after failure.
The PostgreSQL runtime partition passes all eighteen cases, including simultaneous
workers using the same synthetic principal in distinct databases. Each worker
retrieves only its own evidence. These checks make no external model calls.

Final combined recovery checks, GitHub review and exact-head CI remain required
before paid collection. The operational probes suggest about 48–72 hours for
collection with two workers; this is an estimate, excludes subsequent review and
deployment, and does not establish a quality gate or guarantee completion within
the remaining allowance.
