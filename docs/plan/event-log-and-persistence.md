---
title: Event Log and Persistence
status: design
canonical: true
---

# The event log and persistence layer

This expands Sections 6.8, 6.9, 12.2, 14, and 15 of the
[engineering plan](engineering-plan.md), and Milestone 2. It does not replace
them. Where this document adds a table, a column, or a rule, it is an addition
of the kind Section 15 already sanctions when it says to create migrations for
*at least* the tables it lists.

Recorded as [ADR-0003](../adr/0003-event-log-and-projections.md) and
[ADR-0004](../adr/0004-postgres-run-queue.md).

## Why persistence is not a persistence problem

Three specifications written before this one treat the event log as authoritative
and say so in almost the same words. Memory formation is
"a governed projection over the episodic log." Session history is reconstructed
from the log rather than from the previous run's checkpoint (Section 27.4).
Working-state carry is "computed from the log, not copied from the previous
checkpoint" (context engine).

That is three specs whose correctness is downstream of this one. If the log can
lose a write, memory silently forgets. If a projection is not deterministic,
re-derivation produces a different agent than the one the user corrected. If a
sequence can be consumed out of order, a projection can skip an event forever
and no test that reads the log directly will ever notice, because the log is
fine — it is the reader's watermark that is wrong.

So the properties this layer owes the rest of the system are not "durability"
and "performance." They are:

- **Every committed event is eventually observed by every projection, exactly
  once.** Not "written durably" — *observed*. A durable event that a projection
  skipped is, to memory, an event that never happened.
- **A projection rebuilt from an empty state over the same log prefix equals the
  projection built incrementally.** This is what makes re-derivation meaningful
  and what makes a projection safe to throw away.
- **A checkpoint is an optimization, never a source of truth.** Deleting every
  checkpoint must cost time, not information.
- **A run executes at most once, even when the system believes it has crashed
  and is wrong about that.**

The rest of this document is those four sentences made mechanical.

## What the persistence layer must respect

Inherited constraints, all of which predate this document:

- **Never hold a database transaction across external I/O** (Section 12.2). The
  transaction shape is: persist intent, commit, do I/O, persist result, commit.
  This is why the append path below is designed around many short transactions
  rather than one correct long one.
- **Events are append-only** (Section 6.8). Append-only is a statement about
  bytes, not about rows: a stored payload is never rewritten, including by a
  migration. See "Versioning without rewriting" below.
- **The per-session sequence is monotonic and unique** (Section 6.8, Section 15
  `UNIQUE(session_id, sequence)`), and is allocated inside the same short
  transaction that appends the event (Section 27.5).
- **Raw reasoning text is never persisted** (Section 6.8, ADR-0006). Provider-opaque
  continuation lives in the checkpoint for the life of an active tool loop and
  nowhere else.
- **Repository methods that read user-owned data require tenant and principal
  context** (Section 6.2). Every query in this document is implicitly scoped;
  scoping is not optional and is not a ranking input.
- **One active run per session by default** (Section 27.5). This document shows
  that the default is load-bearing for projection correctness, and what has to
  change if it is ever relaxed.

## The append path

### One event, one short transaction

Appending an event is a single transaction containing exactly three statements
and no I/O:

```sql
BEGIN;

UPDATE sessions
   SET next_event_sequence = next_event_sequence + 1
 WHERE id = $session_id
RETURNING next_event_sequence - 1 AS sequence;

INSERT INTO events (session_id, run_id, sequence, event_type,
                    payload_schema_version, actor_type, actor_id,
                    payload, trace_id, created_at)
VALUES (...);

-- optional, same transaction: the state change this event describes
UPDATE runs SET status = $new, updated_at = now()
 WHERE id = $run_id AND status = $expected AND lease_owner = $worker
   AND lease_epoch = $epoch;

COMMIT;
```

Section 27.5 offers two mechanisms for sequence allocation, `SELECT ... FOR
UPDATE` on the session row or an atomic increment of `next_event_sequence`. This
document pins the atomic increment. Both take the same row lock; the increment
holds it for one statement rather than for the caller's whole transaction, and
it makes the lock's existence obvious in the code that must not grow an I/O call
inside it. `UNIQUE(session_id, sequence)` remains the backstop, and a violation
of it is a defect to be fixed rather than a conflict to be retried.

The state change that an event describes belongs in the same transaction as the
event. An event that says `run.completed` while the `runs` row still says
`RUNNING` is a lie the log tells forever. Because the state change is a
conditional `UPDATE` guarded by expected status and lease ownership, a
transaction that loses that race commits nothing at all.

### Gaps are normal; missing writes are not

A transaction can consume a sequence and then roll back. The sequence is not
returned to the pool, so the log contains a gap. This is fine, and every
consumer must be built to tolerate it: **a reader asks for events after a
watermark; it never waits for a specific next sequence to appear.** A projection
that blocks until `N+1` materializes will hang forever on the first rolled-back
append.

The dangerous case is the mirror image, and it is subtle enough to be worth
stating as a scenario rather than a rule.

Two transactions are appending to the same session. Transaction A takes sequence
5. Transaction B takes sequence 6. B commits at 10:00:00.100. A commits at
10:00:00.180. A projection polls at 10:00:00.150, sees sequence 6 and not 5,
processes 6, and stores watermark 6. Event 5 commits thirty milliseconds later
and **is never observed by that projection again**, because the projection only
ever asks for sequences greater than its watermark. The log is perfectly
correct. The `UNIQUE` constraint is satisfied. Memory has silently lost a fact,
and it will be lost identically on every rebuild, so the
rebuild-equals-incremental check passes too.

There are two defences and the system already has the stronger one:

1. **One appender per session.** Section 27.5's default of at most one
   non-terminal run per session means there is exactly one writer allocating
   sequences for a session, so sequences commit in allocation order and the
   interleaving above cannot arise. This is the primary defence, and it is the
   reason the default is load-bearing rather than merely convenient. It is
   enforced by a partial unique index, not by convention.
2. **Snapshot-aware watermarking, if that default is ever relaxed.** A
   projection may only advance its watermark past sequences whose transactions
   are visible to every future snapshot — in PostgreSQL, those with `xmin` below
   `pg_snapshot_xmin(pg_current_snapshot())`. Concretely, the projection reads
   events after its watermark *and* below that horizon, and leaves the rest for
   the next poll.

The rule for implementers: if you ever permit two concurrent appenders in one
session, you have taken on defence 2, and the projection reader must change in
the same commit. A test asserts defence 1 today. If parallel branches are wanted
later, Section 27.5 already directs them to separate sessions or child runs,
which keeps a single appender per sequence space.

### Notification is a hint, never a delivery

ADR-0010 makes PostgreSQL `LISTEN`/`NOTIFY` the transport for worker wakeup and
for live stream delivery. `NOTIFY` is transactional — payloads are delivered at
commit and discarded on rollback — so it does not need an outbox table to avoid
announcing events that never happened.

It does need a rule, because it is at-most-once. A listener that is
disconnected, still connecting, or slow enough to overflow its queue misses the
notification permanently, and nothing retries it.

**No consumer may depend on receiving a notification.** Every consumer is a
poller that a notification makes faster:

- The worker polls the claim query on an interval (250 ms interactive) and
  `NOTIFY` collapses that latency toward zero. If notifications stop entirely,
  throughput degrades to the poll interval and nothing is lost.
- Projections advance from their watermark on a timer, notified or not.
- SSE clients reconnect with `Last-Event-ID` and receive the persisted gap
  before the live stream resumes (Section 16), so a missed notification costs a
  reconnect, not a message.

The failure this rule prevents is the one where everything works in development
and a production connection blip silently drops a turn's worth of memory
formation, because the only thing that would have triggered it was a
notification nobody received.

## Versioning without rewriting

Section 6.8 requires `payload_schema_version` on the envelope and an explicit
upcasting step in the read path. Section 15's `events` table does not currently
carry the column; Milestone 2 requires it, so the migration that creates the
table includes it as `SMALLINT NOT NULL`.

The read path is a registry of pure upcasters keyed by `(event_type,
from_version)`, chained from the stored version to the current one:

```python
class Upcaster(Protocol):
    event_type: str
    from_version: int
    to_version: int

    def upcast(self, payload: dict[str, Any]) -> dict[str, Any]: ...
```

Four rules make this survivable:

- **Stored payloads are never rewritten.** There is no data migration for
  events, only upcasters. A migration that rewrites payloads destroys the
  ability to replay history as it actually was, which is the only reason to keep
  an append-only log in the first place.
- **Upcasters are total and pure.** No I/O, no clock, no lookups against current
  state. An upcaster that reads today's configuration produces a different
  history every time it runs.
- **An upcaster may never invent a value.** If a field did not exist at version
  1, the upcast fills an explicit `None` or a typed `Unknown` sentinel and every
  consumer handles it. Filling a plausible default is how a projection comes to
  assert something the user never said — and memory formation will happily turn
  that into a belief with full provenance pointing at an event that never
  contained it.
- **An unknown *higher* version is an error, not a best-effort decode.** A
  reader running old code against a log written by new code must fail loudly.
  Silent partial decoding of a newer payload is indistinguishable from data
  loss.

Every historical version keeps a recorded fixture in the eval corpus, and the
upcaster chain is exercised against all of them on every build. The gate is
totality: every fixture at every version decodes to the current shape.

## Projections

A projection is a derived, rebuildable read model with a watermark and no
authority of its own. Three exist:

| Projection | Source | Consumer | Rebuild cost |
| --- | --- | --- | --- |
| Session history | `session.*`, `user.message.*`, `assistant.message.*`, `tool.call.*` | Context builder at run seed (Section 27.4) | Per session, bounded by session length |
| Memory | Episodic events at session boundary (ADR-0018) | Retrieval, snapshot assembly (ADR-0019) | Global, expensive, opt-in per principal |
| Trajectory export | Whole runs (Section 31) | Eval fixtures, training data | Per run, cheap |

### The properties every projection has

**Deterministic.** The same log prefix produces the same projection state, byte
for byte, on the same `builder_version`. This is not an aesthetic preference: it
is the precondition for ADR-0018's re-derivation, which replays rejections over
a rebuilt memory and expects the corrections to still hold. A projection that
consults the wall clock, iterates a set in hash order, or reads current
configuration is not rebuildable, and the agent's memory becomes a function of
when it was last rebuilt.

**Watermarked.** Each projection stores its position as `(projection_name,
scope, watermark_sequence, builder_version, updated_at)`, written **in the same
transaction as the projection state it justifies.** If the state and the
watermark can diverge, a crash between them either double-applies or skips.

**Rebuildable from zero.** Rebuild is a first-class, tested operation, not a
recovery script someone writes during an incident. CI rebuilds every projection
over a synthetic log and asserts equality with the incrementally built state.

**Never authoritative.** A projection may be dropped and rebuilt at any time
without user-visible loss beyond the rebuild's duration. Anything that cannot
survive that is not a projection and belongs in the log.

### Derived events and the rebuild loop

Memory formation both consumes the log and emits events onto it —
`memory.formed`, `memory.superseded`, `memory.promoted`. Naively this makes
rebuild non-idempotent: replaying the log re-runs formation, which appends a
second `memory.formed` for the same fact, which the next rebuild replays again.

Derived events therefore carry a **deterministic derivation key**: a hash of the
contributing `source_event_ids`, the rule that fired, and the `builder_version`.
Appends of derived events are conditional on that key being absent
(`ON CONFLICT DO NOTHING` against a unique index). A rebuild re-derives the same
keys and writes nothing new, so it converges instead of multiplying. This is the
same identity discipline ADR-0018 already adopted when it matched rejections by
content rather than by belief id, for the same reason: re-derivation mints new
ids, so ids cannot be the identity.

A consequence worth stating plainly: **changing `builder_version` changes every
derivation key**, so a rebuild after a formation-rule change appends a parallel
set of derived events rather than deduplicating against the old ones. That is
correct — they are different derivations — but it means a rule change is a
migration with a cost, and superseding the old derivations is part of shipping
it, not an afterthought.

## Checkpoints

Section 6.9 already identifies the problem: a checkpoint is written after every
model response and every tool call, and each stores the full conversation, which
is superlinear in run length. It also states the fix. This pins it.

**A checkpoint is a delta against a base.** Full snapshots are written at run
start, at every compaction boundary, and at terminal status. Every other
checkpoint stores the changes since the previous checkpoint. Reconstruction
walks back to the nearest full snapshot and applies deltas forward; the walk is
bounded because compaction forces a full snapshot.

**The conversation is stored as event references.** A checkpoint holds
`event_id` references resolved through the session-history projection, not
inlined message bodies. Two exceptions stay inline because they cannot be
reconstructed from the log:

- **Provider-opaque continuation** (`ProviderContinuation`, `ProviderReasoningItem`).
  Never in the log by rule (ADR-0006), so the checkpoint is the only place it can
  live. It is dropped at run boundaries and when a run is routed to a different
  provider (Section 27.4, ADR-0007).
- **Compacted summary text.** It is authored by a model call, not derived from
  the log, so replaying the log would not reproduce it. It is content, and it is
  covered by the context engine's elision rules.

**Losing checkpoints costs time, not information.** With the conversation stored
as references and the log authoritative, deleting a run's checkpoints and
resuming reconstructs the same state, modulo the two inline exceptions above:
losing an active `ProviderContinuation` forces the current tool loop to restart
from the last full snapshot rather than resume mid-loop. This is a test, not a
hope — "delete the last three checkpoints, resume, assert the same terminal
state" belongs in the resilience suite next to the kill-the-worker test that
Section 14.2 already requires.

**Retention.** After a run reaches a terminal status, prune to the final
checkpoint plus the last full snapshot. Before that, prune deltas older than the
most recent full snapshot. Checkpoint bytes per run is a tracked metric because
it is the one that quietly grows until an incident.

## The run queue

### Claiming, with priority

Section 14.1 specifies `FOR UPDATE SKIP LOCKED`. The revision summary and
ADR-0010 additionally require priority ordering so asynchronous jobs cannot
head-of-line-block interactive turns; Section 14's body does not yet carry it.
The claim query:

```sql
UPDATE runs r
   SET status          = 'RUNNING',
       lease_owner     = $worker_id,
       lease_epoch     = r.lease_epoch + 1,
       lease_expires_at= now() + $lease_duration,
       attempts        = r.attempts + 1,
       updated_at      = now()
  FROM (
        SELECT id
          FROM runs
         WHERE status = 'QUEUED'
           AND (scheduled_for IS NULL OR scheduled_for <= now())
           AND priority = ANY($eligible_classes)
         ORDER BY priority ASC, created_at ASC
           FOR UPDATE SKIP LOCKED
         LIMIT 1
       ) AS c
 WHERE r.id = c.id
RETURNING r.*;
```

Three priority classes, low number first:

| Class | Value | Contents | Latency budget |
| --- | --- | --- | --- |
| Interactive | 0 | A user is waiting on this turn | Seconds |
| Async | 10 | Scheduled and long-running work (Milestone 10) | Minutes |
| Maintenance | 20 | Consolidation, rebuilds, exports | Hours |

Strict priority starves the bottom of the queue, so **capacity is reserved by
class rather than allocated by strict order**: a worker pool is configured with
a minimum concurrency per class, and `$eligible_classes` for a given claim
reflects which of that worker's slots are free. A pool that reserves one slot
for maintenance guarantees consolidation progresses during a busy day, at the
cost of one slot of interactive capacity. Aging was the alternative and it is
worse here — it makes latency depend on queue history, which is exactly the
property that makes a starvation bug reproduce only in production.

The claim query needs `(status, priority, created_at)` as a partial index on
`status = 'QUEUED'`; Section 15's `(status, created_at)` does not serve the
ordering.

### Leases, and the worker that is not dead

The lease protocol is Section 14.1's: claim, set expiry, refresh periodically,
release, and reclaim after expiry. Refresh at one third of the lease duration,
so two consecutive missed heartbeats do not lose the run.

The part that needs adding is what happens when the reclaim is *wrong*. A worker
that is garbage-collecting, swapping, or partitioned from the database is not
dead, and its lease expires anyway. The sweeper reclaims the run, a second
worker starts executing it, and now two processes believe they own the same run.
Lease expiry is a timeout, and a timeout is a guess.

The defence is a fencing token. `lease_epoch` increments on every claim, and
**every write a worker makes is conditional on its own epoch**:

```sql
UPDATE runs SET ... WHERE id = $run_id
  AND lease_owner = $worker_id AND lease_epoch = $my_epoch;
```

An `UPDATE` affecting zero rows means the worker has been fenced. It stops
immediately, does not retry, does not append, and does not treat the zero-row
result as a transient failure — it is the only correct signal that another
worker owns this run now. The same predicate guards the event append's state
change, so a fenced worker cannot write an event either.

This does not prevent a fenced worker from having already begun a side-effecting
tool call, which is why fencing is necessary but not sufficient, and why
recovery leans on tool-level idempotency below.

### Queue-level retry, and what is not a retry

Two different things get called retry and they have different rules:

- **Step-level retry** — a model call or tool call fails and is retried inside
  the run, per Section 13's table, within the run deadline, with exponential
  backoff and jitter. The run never leaves `RUNNING`.
- **Run-level requeue** — a run is returned to `QUEUED` because its lease
  expired and the sweeper reclaimed it. `attempts` increments on claim.

Only lease expiry requeues. A run that fails with a permanent classification
from Section 13 (`ModelPermanentError`, `ToolPolicyDenied`, `AuthorizationError`,
`BudgetExceeded`) transitions to `FAILED` immediately with a typed `failure`
payload; requeueing it would burn attempts against an error that cannot succeed.

`max_attempts` defaults to 3. A run exceeding it goes to `FAILED` with
`failure.reason = "max_attempts_exceeded"` and the accumulated per-attempt
failures retained. There is no separate dead-letter table: `runs` already has a
`failure JSONB` column and a terminal status, and a dead-letter queue whose
entries nothing reads is a table that only ever grows.

`scheduled_for` carries the backoff for a requeued run and doubles as the
scheduling primitive Milestone 10 needs, which is why it lands here rather than
being invented twice.

### Recovery at a safe boundary

On reclaim, Section 14.2's procedure runs against the tool-invocation table
(Section 8.4), which is the authority on what actually executed:

1. Load the latest checkpoint and reconstruct through the delta chain.
2. Read `tool_invocations` for the run at or after the checkpoint's
   `last_event_sequence`.
3. For each invocation not in a terminal state, decide by `IdempotencyClass`:
   read-only and idempotent tools are re-executed; conditionally idempotent
   tools are re-executed only if the stored `idempotency_key` can be replayed
   against the external service; a non-idempotent tool left in `RUNNING` is
   **never** automatically retried.
4. Mark ambiguous non-idempotent executions `UNCERTAIN`, emit
   `tool.call.uncertain`, and surface the run for review rather than guessing.
5. Resume at the first incomplete safe boundary.

`UNCERTAIN` is a terminal state for that invocation, not a transient one. The
model is told, in a structured tool result, that the call's outcome is unknown —
which is true, and which is information it can act on — rather than being told
the call failed, which is a claim the system cannot support and which invites a
duplicate write.

## Schema additions

Section 15 requires migrations for *at least* the tables it lists. These
additions are what Milestone 2's own Implement list and Section 16's contract
already require, plus the columns this document's mechanisms need. No existing
column is removed or retyped.

```text
events
  + payload_schema_version SMALLINT NOT NULL
  + INDEX (session_id, id)               -- watermark scans, commit order

runs
  + priority        SMALLINT     NOT NULL DEFAULT 0
  + attempts        SMALLINT     NOT NULL DEFAULT 0
  + scheduled_for   TIMESTAMPTZ  NULL
  + lease_epoch     INTEGER      NOT NULL DEFAULT 0
  + INDEX (status, priority, created_at) WHERE status = 'QUEUED'
  + UNIQUE INDEX (session_id) WHERE status NOT IN
      ('COMPLETED','FAILED','CANCELLED')    -- 27.5 single active run

idempotency_keys                            -- M2 "Idempotency records"
  key             TEXT PRIMARY KEY          -- Section 16 Idempotency-Key
  tenant_id       UUID NOT NULL
  principal_id    UUID NOT NULL
  request_hash    TEXT NOT NULL
  run_id          UUID NOT NULL
  created_at      TIMESTAMPTZ NOT NULL
  expires_at      TIMESTAMPTZ NOT NULL

projection_watermarks
  projection_name TEXT NOT NULL
  scope           TEXT NOT NULL DEFAULT ''  -- '' global, else session_id
  watermark_seq   BIGINT NOT NULL
  builder_version TEXT NOT NULL
  updated_at      TIMESTAMPTZ NOT NULL
  PRIMARY KEY (projection_name, scope)

derived_event_keys
  derivation_key  TEXT PRIMARY KEY         -- source ids + rule + version
  event_id        BIGINT NOT NULL
  created_at      TIMESTAMPTZ NOT NULL
```

`idempotency_keys` stores `request_hash` so that a repeated `Idempotency-Key`
carrying a *different* body is a `ConflictError` rather than a silent return of
an unrelated run. Section 16 requires that a repeated key return the original
run; it does not say what a reused key with new content means, and returning
someone else's run because a client reused a key is worse than an error.

## Ports and data model

Section 7's `RunRepository` and `EventRepository` are unchanged. Section 7 names
the checkpoint, session, tool-invocation, and usage repositories without typing
them; these are the additions this layer needs.

```python
class NewEvent(BaseModel):
    session_id: UUID
    run_id: UUID | None
    event_type: str
    payload_schema_version: int
    actor_type: str
    actor_id: str | None
    payload: dict[str, Any]
    trace_id: str | None
    derivation_key: str | None = None   # set for derived events only


class CheckpointRepository(Protocol):
    async def write(
        self, run_id: UUID, checkpoint: RunCheckpoint, *, full: bool
    ) -> int: ...
    async def latest(self, run_id: UUID) -> RunCheckpoint | None: ...
    async def prune(self, run_id: UUID, *, terminal: bool) -> int: ...


class ProjectionCursor(BaseModel):
    projection_name: str
    scope: str
    watermark_seq: int
    builder_version: str


class Projection(Protocol):
    name: str
    builder_version: str

    async def apply(
        self, events: Sequence[EventEnvelope], cursor: ProjectionCursor
    ) -> None: ...
    async def rebuild(self, scope: str) -> ProjectionCursor: ...


class RunQueue(Protocol):
    async def enqueue(
        self, run: Run, *, priority: int, scheduled_for: datetime | None
    ) -> None: ...
    async def claim(
        self, worker_id: str, eligible_classes: Sequence[int]
    ) -> tuple[Run, int] | None: ...      # (run, lease_epoch)
    async def heartbeat(
        self, run_id: UUID, worker_id: str, lease_epoch: int
    ) -> bool: ...                        # False means fenced
    async def release(
        self,
        run_id: UUID,
        worker_id: str,
        lease_epoch: int,
        status: RunStatus,
    ) -> None: ...
    async def reclaim_expired(self, limit: int) -> int: ...
```

`heartbeat` returning `False` rather than raising is deliberate: being fenced is
an expected outcome of a normal race, not an exceptional one, and the worker's
handling of it is to stop cleanly rather than to unwind through error paths that
might themselves try to write.

New event types, added to Section 6.8's list:

```text
run.requeued
run.fenced
projection.rebuild.started
projection.rebuild.completed
```

## Failure modes and defenses

| Failure | How it happens | Defense |
| --- | --- | --- |
| Silent missing write | A projection advances its watermark past a sequence whose transaction has not committed yet | One appender per session, enforced by partial unique index; snapshot-aware watermarking if that is ever relaxed |
| Projection stall on a gap | A reader waits for the next contiguous sequence after a rolled-back append | Readers ask for events after a watermark, never for a specific sequence |
| Split-brain worker | A stalled worker's lease expires; the sweeper hands the run to a second worker | `lease_epoch` fencing on every write; zero rows updated means stop, not retry |
| Duplicate turn | A client retries a submit that already succeeded | `idempotency_keys` with `request_hash`; repeat returns the original run, mismatch is a `ConflictError` |
| Notification loss read as event loss | A consumer treats `LISTEN`/`NOTIFY` as delivery | Every consumer polls from a watermark; notification only collapses latency |
| Upcaster invents data | A missing field is filled with a plausible default | Sentinels only; consumers handle unknown explicitly; unknown higher version is a hard error |
| History rewritten by migration | A payload shape change is applied as an `UPDATE` over `events` | Stored payloads immutable; change is expressed only as an upcaster |
| Derived-event multiplication | A rebuild re-emits `memory.formed` for facts already derived | Deterministic derivation key, conditional append, convergent rebuild |
| Checkpoint growth | Full conversation inlined at every tool call | Deltas against periodic full snapshots; conversation as event references |
| Head-of-line blocking | A long async run occupies the workers a user is waiting on | Priority classes with capacity reserved per class |
| Non-idempotent double write | A tool left `RUNNING` by a crash is retried | Idempotency class decides; ambiguous cases become `UNCERTAIN` and stop |
| Watermark/state divergence | Projection state committed separately from its cursor | Both written in one transaction |

## Hard gates

Milestone 2 does not pass until every one of these holds.

1. **Sequence integrity.** A fuzz test appending concurrently across sessions,
   with injected rollbacks, produces no duplicate `(session_id, sequence)` and no
   event that any projection failed to observe. **M2.**
2. **Projection determinism.** For every projection, rebuild-from-zero over a
   recorded log equals the incrementally built state, field for field, on the
   same `builder_version`. **M2.**
3. **Upcaster totality.** Every recorded historical fixture, at every version,
   decodes to the current shape. An unknown higher version raises. **M2.**
4. **Exactly-once execution.** Two workers racing on one run: one executes, the
   other is fenced and writes nothing. Asserted with the sweeper's reclaim
   interval driven to zero. **M2.**
5. **Crash recovery.** Terminate a worker after a checkpoint; the run resumes and
   reaches the same terminal state. Section 14.2 already requires this; it is
   restated here because the delta-chain reconstruction is new and it is what the
   test now exercises. **M2.**
6. **Checkpoint dispensability.** Delete a run's non-terminal checkpoints,
   resume, and reach the same terminal state. Registered as
   `gate.event.checkpoint_dispensable`, which `runtime-loop.md` #9 restates:
   this document owns it. **M2.**
7. **Transaction hygiene.** A static check plus a runtime assertion: no
   transaction is open across an `await` that performs provider, tool, or sandbox
   I/O. Registered as `gate.structure.txn_hygiene`, which `runtime-loop.md` #6
   restates: this document owns it. **M2.**

## Tracked metrics

Claim latency p99 by priority class, lease reclaim rate (a rise means leases are
too short or workers are stalling), projection lag in sequences and seconds,
checkpoint bytes per run, and rebuild duration per projection.

## Build sequence

1. **Schema and migrations.** Section 15's tables plus the additions above.
   Nothing consumes `payload_schema_version`, `priority`, or `lease_epoch` yet;
   they are cheap now and are retrofits that break replay later.
2. **Append path.** Sequence allocation, envelope insert, conditional state
   change, all in one transaction. Fuzz for sequence integrity before anything
   depends on it.
3. **Upcaster registry.** With the first two versions of one event type
   recorded, so the mechanism exists before it is needed under pressure.
4. **Checkpoints.** Full snapshots first, then deltas, then reference-based
   conversation storage. Each step keeps the dispensability test passing.
5. **Queue.** Claim, lease, heartbeat, fencing, sweeper. Then priority classes
   and reserved capacity.
6. **Session-history projection.** The one Section 27.4 requires for run seeding;
   it makes cross-run continuity real and is the first consumer to exercise
   watermarks.
7. **Recovery.** Tool-invocation-driven resume, idempotency classes, `UNCERTAIN`.
8. **Trajectory-export projection scaffold.** Structure and watermark only; the
   export itself is Milestone 3.
9. **Rebuild.** Rebuild-from-zero for every projection, wired into CI as a gate
   rather than as a script.

## Decisions

1. **The log's contract is observation, not durability.** A committed event that
   a projection never observed is, to every consumer, an event that did not
   happen; the layer's gates are written against that.
2. **Sequence allocation is an atomic increment of `sessions.next_event_sequence`**
   inside the appending transaction, with `UNIQUE(session_id, sequence)` as a
   backstop whose violation is a defect rather than a retryable conflict.
3. **Sequence gaps are legal and readers tolerate them.** Consumers read after a
   watermark and never wait for a specific next sequence.
4. **One appender per session is load-bearing for projection correctness**, not
   only for contention. Relaxing Section 27.5's default requires switching
   projections to snapshot-aware watermarking in the same change.
5. **`LISTEN`/`NOTIFY` is a latency optimization and never a delivery
   guarantee.** Every consumer is a poller first.
6. **Stored event payloads are immutable**; schema evolution is expressed only
   as pure, total upcasters, and an upcaster may never invent a value.
7. **An unknown higher payload version is a hard error.** Old code must not
   partially decode new events.
8. **Projections are deterministic, watermarked, rebuildable, and never
   authoritative**, with state and watermark written in one transaction.
9. **Derived events carry a deterministic derivation key** and append
   conditionally, so rebuilds converge instead of multiplying.
10. **Checkpoints are deltas against periodic full snapshots**, with the
    conversation stored as event references; only provider-opaque continuation
    and compacted summary text stay inline.
11. **Losing checkpoints costs time, not information**, and that is a test.
12. **The claim query orders by priority then age**, across three classes, with
    capacity reserved per class rather than strict priority, so maintenance work
    cannot starve.
13. **Lease expiry is a guess, so every worker write is fenced by `lease_epoch`**;
    a zero-row update means stop, not retry.
14. **Only lease expiry requeues a run.** Permanent Section 13 classifications
    fail immediately; `max_attempts` is 3; `runs.failure` is the dead letter.
15. **`scheduled_for` carries both retry backoff and Milestone 10 scheduling**,
    so the primitive is built once.
16. **Ambiguous non-idempotent tool executions become `UNCERTAIN`** and are
    reported to the model as unknown-outcome rather than as failed.

## Open questions

None blocking Milestone 2. Two recorded for Andy's review, with the interim
decision noted in
[questions for review](../status/questions-for-review.md):

- Whether single-active-run-per-session should be promoted from Section 27.5's
  "default" to an invariant, given that projection correctness now depends on
  it. The interim position keeps Section 27.5's wording and documents the
  coupling.
- Whether the trajectory-export projection belongs in Milestone 2 or Milestone 3.
  Section 21's Implement list says Milestone 2; Section 21.1's sequencing table
  says Milestone 3. The interim split is scaffold in 2, export in 3.
