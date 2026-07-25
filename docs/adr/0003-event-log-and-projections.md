# ADR-0003: Event log, projections, and checkpoints

- Status: Proposed (amended 2026-07-24)
- Date: 2026-07-24
- Related: Milestone 2 (durable execution), Sections 6.8 (event envelope),
  6.9 (checkpoint), 12.2 (loop persistence), 14 (durable execution),
  15 (data model), 27.4/27.5 (cross-run continuity, one active run per
  session), ADR-0009 (run/turn/session), ADR-0016 (trajectory capture),
  ADR-0018/0019 (memory write and read paths)
- Detailed design: `docs/plan/event-log-and-persistence.md`

## Context

The engineering plan names an append-only event log with normalized projections
as a foundational decision and specifies the envelope in Section 6.8, the
`events` and `checkpoints` tables in Section 15, and durable execution in
Section 14. What it does not specify is the set of properties that make the log
usable as a foundation: what a consumer is entitled to assume when it has read
up to a sequence number, how a payload shape changes without rewriting history,
what a projection guarantees, and what happens when a projection is rebuilt.

The word *rebuild* does not appear anywhere in the plan, yet every downstream
subsystem depends on rebuilds being possible and convergent. ADR-0018 makes the
memory store a governed projection over the episodic log; ADR-0019 has retrieval
reading from it; ADR-0009 seeds a new run from a session-history projection
rather than from the previous run's checkpoint. Each of those assumes a rebuild
produces the same state as incremental application, and none of them can be
tested for it until the log says so.

There is also a specific hazard that no log-level test catches. Two appends take
sequence numbers 5 and 6; the transaction holding 6 commits first. A projection
polling in that window sees 6, advances its watermark, and never observes 5 when
it commits. The log is internally consistent, `UNIQUE(session_id, sequence)` is
satisfied, and every rebuild reproduces the same loss identically. The failure is
in the reader's definition of progress, not in the writer.

## Decision

1. **The log's contract is observation, not durability.** A committed event that
   no projection ever observed is, to every consumer, an event that did not
   happen. The layer's hard gates are written against observation, because
   durability is the property that is easy to test and observation is the one
   that is actually load-bearing.
2. **Sequence allocation is an atomic increment of
   `sessions.next_event_sequence`** performed inside the appending transaction.
   `UNIQUE(session_id, sequence)` remains as a backstop, but a violation is a
   defect to be fixed rather than a conflict to be retried.
3. **Sequence gaps are legal, and readers tolerate them.** A rolled-back append
   burns its number. Consumers read *events after a watermark*, never *the event
   at sequence n + 1*, so a gap costs nothing and a reader can never stall
   waiting for a number that will never exist.
4. **One appender per session is load-bearing for projection correctness**, not
   merely for contention. Section 27.5's "one active run per session" default is
   what makes a monotonic sequence watermark safe. Relaxing that default requires
   switching every projection to snapshot-aware watermarking
   (`pg_snapshot_xmin(pg_current_snapshot())`) **in the same change**, and this
   ADR is the record of that coupling.
5. **`LISTEN`/`NOTIFY` is a latency optimization and never a delivery
   guarantee.** It is transactional, so no outbox table is needed; it is
   at-most-once, so every consumer is a poller first and treats a notification
   only as permission to poll sooner.
6. **Stored event payloads are immutable.** Schema evolution is expressed only as
   **pure, total upcasters** from version *n* to version *n + 1*, composed on
   read. No migration rewrites `events`.
7. **An upcaster may never invent a value.** A field absent from an older payload
   is filled with an explicit sentinel that consumers must handle, not with a
   plausible default that silently backdates a decision nobody made.
8. **An unknown higher payload version is a hard error.** Old code must refuse to
   partially decode a newer event rather than proceed on the fields it happens to
   recognize.
9. **Projections are deterministic, watermarked, rebuildable, and never
   authoritative.** Projection state and its watermark are written in **one
   transaction**, so a crash cannot leave state ahead of its cursor or behind it.
   Rebuild-from-zero equalling incremental application, field for field, is a hard
   gate rather than an aspiration.
10. **Derived events carry a deterministic derivation key** and are appended
    conditionally on that key. A projection that emits events — memory formation
    is the first — therefore converges on rebuild instead of multiplying its own
    output on every replay.
11. **Checkpoints are deltas against periodic full snapshots**, with the
    conversation stored as event references rather than inlined text. Only
    provider-opaque continuation state and compacted summary text stay inline,
    because neither can be reconstructed from the log.
12. **Losing checkpoints costs time, not information.** Deleting a run's
    non-terminal checkpoints and resuming must reach the same terminal state, and
    that is an executable test rather than a design intention.

**Amendment (2026-07-24).** `events` gains `payload_schema_version SMALLINT NOT
NULL`, which Section 6.8 and Milestone 2 both require and which Section 15's
table omits. It is written from the first migration, before anything consumes it:
it is one column now and an unmigratable retrofit once a log exists. `events`
also gains an index on `(session_id, id)` to support watermark reads, and two
tables are added — `projection_watermarks` (with `builder_version`, so a builder
change forces a rebuild rather than a silent reinterpretation) and
`derived_event_keys`.

## Consequences

- The log becomes testable at the level that matters. Sequence integrity under
  concurrent appends with injected rollbacks, projection determinism, and
  upcaster totality are three hard gates on Milestone 2 that did not exist
  before, and they are the gates that catch the failures that are otherwise
  invisible.
- Section 27.5's default acquires a second reason to exist, and a documented
  cost to relaxing. A future decision to allow concurrent runs per session is now
  a decision about projection correctness, which is the honest framing.
- Every consumer of the log carries polling machinery even where notification is
  available, which is slightly more code in exchange for a system that degrades in
  latency rather than in correctness when notification is lost.
- Upcasters accumulate. The registry is written in Milestone 2 with two versions
  of one event type recorded, so the mechanism and its fixtures exist before the
  first urgent schema change rather than during it.
- Checkpoint reconstruction becomes a delta-chain walk, which is more code in the
  resume path and is what the crash-recovery test now exercises. Section 14.2
  already required the test; its subject has changed.
- Memory formation, trajectory export, and session-history seeding all become
  ordinary projections with the same four properties, so the rebuild gate covers
  them without additional per-subsystem argument.

## Alternatives considered

- **Treating the log as the durable record and testing only that writes
  commit**: rejected; the missing-write hazard above passes every durability
  test, is reproduced identically by every rebuild, and surfaces as a subtly
  wrong projection rather than an error.
- **Assigning sequence numbers from a sequence object or `max(sequence) + 1`**:
  rejected; a Postgres sequence is non-transactional and produces gaps that are
  indistinguishable from lost writes without a per-session counter to compare
  against, and `max + 1` serializes on read and races under concurrency.
- **Requiring contiguous sequences by having readers wait for gaps to fill**:
  rejected; a rolled-back transaction leaves a hole that will never fill, and the
  reader stalls permanently on a number that does not exist.
- **An outbox table for event publication**: rejected as unnecessary rather than
  as wrong. `NOTIFY` is delivered on commit and rolled back with its transaction,
  so the atomicity an outbox exists to provide is already present; what it does
  not provide is delivery, and the watermark poll covers that at lower cost than a
  second table with its own drainer.
- **Migrating stored payloads in place when a shape changes**: rejected; it makes
  the log a mutable structure, defeats replay against historical fixtures, and
  turns every schema change into a data-loss risk over the system's own history.
- **Defaulting missing fields in upcasters to the value the code would have used
  at the time**: rejected; it is indistinguishable from a real recorded value
  downstream, which is precisely the property that makes it dangerous.
- **Best-effort decoding of unknown higher versions**: rejected; during a rolling
  deploy this silently produces two populations of readers disagreeing about the
  same event.
- **Making projections authoritative and skipping the rebuild gate**: rejected;
  it removes the property that lets ranking, extraction, and consolidation logic
  change at all, which every memory decision in ADR-0018 and ADR-0019 depends on.
- **Emitting derived events unconditionally and de-duplicating downstream**:
  rejected; it makes replay non-idempotent and pushes the correctness burden onto
  every consumer instead of the one producer.
- **Full checkpoints at every step**: rejected on growth; a long tool-heavy run
  inlines the same conversation repeatedly. Deltas plus event references keep the
  checkpoint proportional to what changed.
