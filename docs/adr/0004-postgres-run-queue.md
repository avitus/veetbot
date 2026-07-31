# ADR-0004: The Postgres run queue, leases, and recovery

- Status: Proposed
- Date: 2026-07-24
- Related: Milestone 2 (durable execution), Sections 12.2 (loop persistence),
  13 (failure taxonomy), 14 (durable execution), 15 (data model),
  16 (`Idempotency-Key`), 21 (milestones), 27.5 (one active run per session),
  ADR-0009 (run/turn/session), ADR-0003 (event log and projections)
- Detailed design: `docs/plan/event-log-and-persistence.md`

## Context

Section 14 specifies a Postgres-backed run queue claimed with `FOR UPDATE SKIP
LOCKED` and a lease that expires. That is enough to distribute work and not
enough to run work exactly once. A lease expiring is a *guess* that a worker is
dead; the worker may be alive, slow, and about to write. The revision summary
requires claim-priority ordering, but Section 14's body has neither priority text
nor a `priority` column in Section 15's `runs` table, so an interactive turn
queues behind whatever long async work arrived first. Section 16 promises
`Idempotency-Key` and Milestone 2 lists "Idempotency records", but no table
exists to hold them. And there are no queue-level retry semantics at all: no
attempt cap, no dead letter, no statement of what happens to a run whose lease
expired versus one that failed.

Choosing Postgres over a dedicated broker was already decided. The open questions
are the ones that decide whether the choice holds up: what makes execution
exactly-once when the failure detector is unreliable, and what keeps a user
waiting on a chat turn from queueing behind a four-hour research run.

## Decision

1. **The claim query orders by priority, then age**, across three classes —
   interactive (0), async (10), maintenance (20) — selected with `FOR UPDATE SKIP
   LOCKED` in a scalar subquery so contending workers step over each other rather
   than serialize.
2. **Starvation is prevented by reserving capacity per class, not by aging.** A
   pool reserves worker slots for each class and each worker claims only from the
   classes it is eligible for. Aging was rejected because it makes a run's latency
   depend on the history of the queue, which is exactly the property that makes
   queue behaviour impossible to reason about after an incident.
3. **Lease expiry is a guess, so every worker write is fenced.** `runs` carries a
   `lease_epoch` that increments on every claim; every write a worker makes
   asserts its own epoch in the `WHERE` clause. **A zero-row update means stop,
   not retry** — the run belongs to someone else now, and the fenced worker
   records `run.fenced` and abandons the run without touching it further.
4. **`heartbeat` returns `False` when fenced rather than raising.** Losing a lease
   is an expected outcome of a slow turn, not an exceptional one, and a return
   value forces the caller to handle it at the point where continuing would be
   wrong.
5. **Only lease expiry requeues a run.** A Section 13 permanent classification
   fails immediately and is never retried by the queue. `max_attempts` is 3;
   `runs.failure` is the dead letter, so a failed run stays visible in its own
   table rather than moving to a separate one nobody reads.
6. **`scheduled_for` carries both retry backoff and Milestone 10 scheduling.**
   One nullable timestamp and one predicate in the claim query serve both, so the
   scheduling primitive is built once and exercised from Milestone 2.
7. **Recovery resumes from the last checkpoint at a tool-invocation boundary**,
   and what happens next is decided by the tool's declared idempotency class.
8. **Ambiguous non-idempotent tool executions become `UNCERTAIN`** and are
   reported to the model as an unknown outcome rather than as a failure. "It
   failed" is a claim the runtime cannot support about a payment that may have
   been made, and the model handles uncertainty better than it handles a
   confident falsehood.
9. **Idempotency is a table, not a convention.** `idempotency_keys` stores the
   key, tenant, principal, a `request_hash`, and the resulting `run_id`. A repeat
   with a matching hash returns the original run; a repeat with a differing hash
   is a `ConflictError`, never a second run.
10. **The `runs` table enforces Section 27.5 in schema** — a partial unique index
    on `session_id` where status is non-terminal. Section 27.5 states the rule in
    prose; ADR-0003 makes projection correctness depend on it; a constraint is
    what makes it true.
11. **The queue emits events.** `run.requeued` and `run.fenced` join Section 6.8's
    list, so lease loss and reclaim are visible in the log rather than only in
    worker logs.

## Consequences

- Exactly-once *state transition* becomes assertable, which is a narrower claim
  than exactly-once execution and is the one this design can actually make. Two
  workers racing on one run, with the sweeper's reclaim interval driven to zero,
  must end with one committing its transitions and the other writing nothing —
  a hard gate on Milestone 2. External effects are not covered by it: the
  residual-risk bullet below says why, and the guarantee there is at-most-once
  only for effects behind an idempotency class.
- Every worker write path gains an epoch predicate and a zero-row branch. This is
  invasive by design: a write that cannot be fenced is a write that can happen
  twice, so the compiler-visible cost is the point.
- Fencing constrains where work may be committed. A worker that has already
  performed an external side effect and *then* discovers it is fenced cannot undo
  it; the design keeps side effects behind the tool idempotency class and the
  `UNCERTAIN` outcome, which is the residual risk this decision accepts rather
  than eliminates.
- Reserved capacity means a pool is never fully utilized by one class, so
  throughput on a saturated system is slightly lower than strict priority would
  give, in exchange for a p99 interactive claim latency that does not depend on
  what else is queued.
- `runs` gains four columns (`priority`, `attempts`, `scheduled_for`,
  `lease_epoch`), the partial unique index enforcing one non-terminal run per
  session, and a partial index on
  `(status, priority, created_at)`. All are written in the first migration; a
  fencing token retrofitted after a queue exists cannot be applied to in-flight
  runs.
- The single-active-run constraint will surface as a user-visible error the first
  time a client submits a turn while one is *running*. That is the correct
  behaviour under ADR-0009 (`WAITING_FOR_USER` is a state, not a queue), and it
  needs an error class the API layer handles deliberately.
- `WAITING_FOR_USER` is the case that constraint must **not** reject, and it is
  worth stating because the index cannot tell the two apart. It is non-terminal,
  so it holds the session's one active-run slot; a user answering
  `conversation.ask_user` would therefore be unable to create a run at all. The
  answer does not create one. The submit handler's routing decision resolves to
  input delivery rather than to a new run, which resumes the waiting run in
  place, and the conflict error is reserved for a submit against a run that is
  genuinely executing. A design that routed the answer to a new run would
  deadlock every question the agent asks.
- Priority classes are three fixed integers rather than a scheduling policy. When
  Milestone 10 adds scheduled work at scale this may need revisiting; the column
  is a `SMALLINT`, so the classes can subdivide without a migration.

## Alternatives considered

- **A dedicated broker (Redis, SQS, RabbitMQ, Temporal)**: rejected, consistent
  with the plan's existing decision. The queue and the event log share one
  transaction boundary; splitting them across two systems reintroduces exactly the
  dual-write problem the append path is designed to avoid, and adds an operational
  dependency before there is load to justify it.
- **Trusting lease expiry without fencing**: rejected; it makes correctness depend
  on the accuracy of a timeout, and the failure it permits — two workers executing
  one run, both writing — is the failure the queue exists to prevent.
- **Raising an exception on lost lease instead of returning `False`**: rejected;
  an expected control-flow outcome expressed as an exception is either caught too
  broadly or not at all, and the wrong handling here means writing after being
  fenced.
- **Strict priority ordering**: rejected; maintenance work starves indefinitely
  under sustained interactive load, and starvation of the compaction and
  consolidation jobs degrades the system silently.
- **Aging queued runs into higher priority classes**: rejected; latency becomes a
  function of queue history, which cannot be reasoned about from a single run's
  record during an incident.
- **Separate queues or separate tables per priority class**: rejected; it
  multiplies the claim, lease, sweep, and fencing code by the number of classes to
  save one indexed integer comparison.
- **A dead-letter table**: rejected; `runs.failure` with a terminal status keeps
  the failure attached to the run's own history, its events, and its checkpoints.
  A separate table means reconstructing that context to answer any question about
  it.
- **Retrying permanent failures with backoff anyway**: rejected; Section 13
  already classifies failures, and retrying a permanent classification burns
  attempts, delays the user's error, and can repeat a partial side effect.
- **Reporting ambiguous non-idempotent executions as failures**: rejected; it is
  an unsupported claim, and it invites the model to retry an operation that may
  already have succeeded.
- **Idempotency by natural key on the request body**: rejected; body-hash-only
  matching collides across principals and cannot distinguish an intentional
  identical resubmission from a client retry. The explicit key plus the hash
  distinguishes both cases and detects key reuse with different content.
