# ADR-0023: The run loop, the step, and the single terminal writer

- Status: Accepted
- Date: 2026-07-25
- Related: Milestones 1, 2, 4, 5, 7, Sections 6.4 (the run record), 6.5
  (limits and usage), 6.9 (checkpoints), 12 (the runtime), 13 (failure
  handling and retries), 14 (concurrency and locking), 16
  (observability), 19 (deployment), 26 (the demonstration trace), 27
  (run, turn, and session), ADR-0002 (provider-neutral model protocol),
  ADR-0003 (event log and projections), ADR-0004 (the Postgres run
  queue), ADR-0005 (the deterministic policy engine), ADR-0009 (run,
  turn, and session model), ADR-0020 (context engine), ADR-0021 (tool
  execution pipeline), ADR-0022 (the gate registry)
- Detailed design: `docs/plan/runtime-loop.md`

## Context

Section 12.1 specifies the runtime as forty-two lines of pseudocode. Those
lines call eleven named functions, and not one of the eleven is a declared
port. Section 7 declares eight port Protocols with full signatures; the
only one the loop touches is `context_builder.build`. Everything else at
the center of the runtime — claiming the run, loading the checkpoint,
resolving the agent version, resolving the principal, checking
cancellation, checking budget, invoking the model, recording usage,
processing tool calls, selecting the final message, completing the run —
is a name with no contract.

That would be an ordinary documentation gap if the missing pieces were
inferable. They are not, because each name is a seam where a different
specification's requirements arrive, and at most of those seams more than
one specification has an opinion and the opinions differ.
`invoke_model_and_persist_events` is where Section 13's retry table meets
the model gateway's rule that retry ownership splits on
`stream_had_output`. `process_tool_calls` is where the tool system's
fourteen-step pipeline meets the policy engine's approval suspension.
`budgets.check_before_step` is where Section 6.5's *"check limits before
and after every model or tool operation"* meets ADR-0002's *"budget is
checked before an attempt, and failed attempts count against budget"* —
two mandatory rules at different granularities, with the pseudocode
implementing a third that is neither. There are twenty such collisions,
and an implementer meets all twenty in the first week.

Three of them are not disagreements about detail. They are places where
the loop as written cannot do what another document requires of it. It
cannot resolve its own agent: line 1408 reads
`agents.get_version(run.agent_id, run.agent_version)` and neither field
exists on `Run`, because Section 6.3 puts both on `Session`. It cannot
suspend: line 1437 handles a paused disposition with a bare `return`,
while Section 27.2 requires that entering either `WAITING_*` state release
the lease, checkpoint, and emit an event — so a run that pauses for
approval holds its lease until the lease expires, at which point the queue
reclaims it and a second worker begins executing a run that is waiting for
a human. And it cannot compact: the context engine assigns compaction to
the loop in one sentence, Section 11.4 requires it, Milestone 7 gates it,
and no pressure measurement or compactor call appears anywhere in the
corpus.

The seven specs written before this one each declared their own interior
and left their loop-facing edge to the runtime. This is the document where
those edges have to meet, and it is the last one that can be written
before code starts, because every other spec's Milestone 1 deliverable is
called from it.

## Decision

1. **The loop computes an outcome; a single executor performs every
   terminal action.** `run_loop` returns a `RunOutcome` and never
   transitions a run, releases a lease, or appends a terminal event.
   `finalize` does all three, in one module, for all five outcome kinds:
   completed, failed, cancelled, suspended, fenced. The suspension bug is
   not fixed by adding a `finally` to the existing loop — it is fixed by
   moving the ending somewhere a `return` cannot skip.
2. **The five outcome kinds are exhaustive and typed.** `SUSPENDED`
   carries a `Suspension` naming the invocation that paused and the
   approval, question, or child runs it waits on. `FAILED` carries a
   `RunFailure` with one of fourteen `FailureReason` values. A terminal
   state that cannot say why it was reached is an operational dead end,
   and "failed" alone does not distinguish a provider outage from an
   exhausted budget from a policy denial.
3. **`Step` is a runtime value object with a persisted identity, not a
   table.** One additive column, `model_calls.step_number`, gives every
   step an identity that survives the process, including steps that
   produce no tool calls and are currently invisible. A `steps` table
   would duplicate `model_calls` row for row.
4. **`Turn` gets no domain object.** `run == turn`, as ADR-0009 already
   decided; a `Turn` model would be a second name for a run row and a
   second place for its status to be wrong.
5. **Nine fields are added to `Run`.** Six are columns the event-log spec
   already introduced and never reflected in the domain model
   (`tenant_id`, `lease_epoch`, `attempts`, `priority`, `scheduled_for`,
   `failure`); three — `agent_id`, `agent_version`, `deadline_at` — are
   denormalized from `Session` and `RunLimits` so the run is
   self-describing and the deadline is indexable by the sweep.
6. **`Clock` and `IdFactory` are ports, and ambient time or identity is a
   structural-gate violation.** The evaluation harness pins both to make
   runs reproducible; the runtime is their heaviest consumer, so this is
   where they are declared.
7. **The agent version is pinned at run creation and never re-resolved.**
   A deploy that lands while a run waits on an approval does not change
   the agent underneath the person approving it.
8. **The principal is resolved once per execution.** The single exception
   is approval resumption, where the policy engine revalidates by design,
   because the authorization it is about to act on was granted by a human
   whose entitlements may have changed since.
9. **One `CancellationToken` per run serves the loop, the tool executor,
   and the sandbox, with six observation points and one rule about
   effects.** A cancellation observed after a call's effect watermark is
   set does not abandon the call; the run finishes the disposition and
   then stops. Cancelling into a half-sent side effect is worse than
   taking one more second to stop cleanly.
10. **Budget has three scopes — run, step, attempt — and "after" means
    "record".** Recording usage and evaluating the limit are one operation
    in one transaction, because splitting them opens a window in which the
    run is over budget and nothing in the system knows it yet.
11. **The heartbeat is a supervisor task, not a statement in the loop, and
    it watches three things.** One timer at a third of the lease interval
    renews the lease, checks the deadline, and polls for a cancellation
    request. `heartbeat` returns `bool`, and `False` means fenced. Three
    concerns that are all "has the outside world changed its mind" get one
    timer and one query rather than three.
12. **A fenced worker aborts its in-flight model stream and appends
    exactly one event.** Aborting is safe because nothing the stream
    produced could have been committed under a stale epoch; the single
    `run.fenced` append is legal because the event log is sequence-guarded
    rather than epoch-guarded.
13. **Every write the loop makes outside the event append is guarded by
    `WHERE lease_epoch = :lease_epoch`.** Fencing is not a convention
    about well-behaved workers; it is a predicate on every UPDATE, and a
    zero-row result is how a worker learns it has been superseded.
14. **Compaction happens in the loop, before `build()`, capped at two per
    step.** `build_with_pressure` measures, compacts if the body will not
    fit, adopts the returned checkpoint, and measures again;
    `ContextOverflow` after two attempts is a permanent failure.
    Compaction is a write, not a side effect of assembly, which is why it
    lives at the call site rather than inside `build()`.
15. **The pinned tool set reaches `build()` through `run.session_id` and
    `ContextPlanner.current`.** Section 7's four-parameter signature is
    unchanged; the alternative was a fifth parameter that every caller
    would have had to compute identically.
16. **The checkpoint has a stored form and a materialized form, and both
    existing statements about it are true of different types.** Section
    6.9's inline `conversation` describes what the repository returns;
    event references and deltas describe what it persists. `full` gets a
    rule the call site can evaluate — version 1, every eighth version,
    compaction, suspension, terminal — and `checkpoints` gains `full` and
    `base_version`.
17. **Checkpoints are dispensable by construction.** `seed_checkpoint` is
    a function with two call sites, so deleting every checkpoint a run has
    written and resuming from the event log reaches the same terminal
    state. A checkpoint is a cache with a strong consistency requirement,
    and a cache the system cannot lose is a second source of truth.
18. **Suspension is one mechanism with three kinds, and a child-run wait
    reuses `WAITING_FOR_APPROVAL`.** Adding a fourth run status to the
    state machine for a wait that behaves identically buys a more honest
    status name at the cost of amending every exhaustive match over
    `RunStatus`. This is the decision in this ADR least likely to survive
    contact with a reader, and it is flagged for review.
19. **Resumption is a cold process start and a warm pipeline entry.** The
    worker claims a `QUEUED` run from scratch with no in-memory
    continuation; the tool pipeline re-enters at step 6 for each pending
    call, so a call whose watermark was already set is not re-executed.
    `run.resumed` is emitted whenever the execution did not start the run,
    which covers all four resume paths without enumerating them.
20. **An empty terminal model turn is retried as a failed step and fails
    the run with `EmptyModelTurn` on exhaustion.** A turn with no content
    and no tool calls is not a completed run with an empty answer; it is a
    provider anomaly, and returning it to the user as the final message is
    the worst available outcome.
21. **Post-run hooks are enqueued after the terminal transition commits
    and can never fail the run.** Memory formation, trajectory export, and
    notification are consequences of a finished run, not conditions of
    finishing it. The child-run join is the sole exception, because it is
    part of the parent's lifecycle rather than downstream of it.
22. **This document adds no event types.** It consolidates fourteen
    introduced elsewhere and assigns owners to the three that had none:
    `run.claimed` and the two `run.waiting_*` events.
23. **Section 26's demonstration trace is a subsequence, not a
    specification of checkpoint frequency.** An implementation that
    checkpoints after every model response satisfies it; one that
    suppresses checkpoints to match it literally has broken its own
    recovery to match a worked example.
24. **All twenty contradictions are resolved in the spec by number, with
    the losing side named.** Where two documents disagree, one is declared
    canonical explicitly. The evaluation harness's `tool.proposed` /
    `tool.authorized` / `tool.succeeded` case is simply wrong — Section
    6.8's `tool.call.*` names are canonical — and that case is corrected
    rather than accommodated.
25. **Fourteen hard gates enforce the decisions that erode quietly.** The
    load-bearing ones are the single terminal writer (one module may call
    `transition` and `release`), the lease released at most once per
    execution including the crash and fence cases, no transaction held
    across external I/O, a waiting run holding no lease and no slot, and
    no cancelled run with a sent effect. Each of these is a property that
    holds on the day it is written and stops holding six months later
    without anyone deciding it should.

## Consequences

- The runtime stops being the only major component whose interface surface
  is undeclared. Eleven names become ports or named functions with
  signatures, which is what makes the Milestone 1 vertical slice
  implementable by someone who was not in the design conversation.
- The suspension defect is closed structurally rather than by a patch. A
  paused run releases its lease exactly once, and the gate that asserts a
  single terminal writer is what keeps a future `return` from reopening
  it.
- Compaction acquires a call site, which was the last hard gate in
  Milestone 7 with no code path to attach to.
- Three additive schema changes land: one column on `model_calls`, two on
  `checkpoints`, and nine fields on the `Run` domain model of which six
  already existed as columns. None is a migration of existing data,
  because there is no existing data.
- Determinism becomes enforceable. With `Clock` and `IdFactory` as ports
  and ambient use gated, a replayed run produces the same identifiers and
  timestamps, which is a precondition for the harness's replay cases
  rather than a nicety.
- The executor and the loop are separate modules that must be read
  together to understand a run's ending. That is a real cost in
  comprehensibility, accepted because the alternative distributes terminal
  writes across every exit path in a loop that has five of them.
- Six questions are recorded as open. The two with the highest reversal
  cost are whether cancellation ships in three milestone slices or waits
  entirely for Milestone 5, and whether a child-run wait deserves its own
  status. Both are cheap now and expensive after the state machine has
  consumers.

## Alternatives considered

- **Leaving the eleven names to the implementer**: rejected. They are not
  local details; each one is a place where two specifications disagree,
  and an implementer resolving them silently makes twenty architectural
  decisions with no record. The plan's own rule is that a material
  architectural decision requires an ADR, and this is twenty of them.
- **Adding a `finally` to the existing flat loop**: rejected as the
  minimal fix that does not hold. It closes the lease leak on the paths
  that exist today and leaves the next `return` free to reopen it, and it
  offers nothing to gate against. Separating outcome from ending gives the
  property a single enforcement point.
- **A `steps` table**: rejected. It would carry the same identity,
  timing, and usage as `model_calls` for every step that makes a model
  call, which is all of them, and it would need its own consistency rule
  against the table it duplicates.
- **A `Turn` domain object**: rejected again here, consistent with
  ADR-0009. Every field it would carry is already on the run row, and two
  objects with one lifecycle means two places for the status to disagree.
- **Compaction inside `ContextBuilder.build()`**: rejected. It would make
  `build()` non-idempotent and give it a write, which breaks the
  byte-identical rebuild the step-retry path depends on and makes the
  context engine's own contract suite untestable without a database.
- **A dedicated `WAITING_FOR_CHILD` run status**: rejected for 0.1 and
  recorded as an open question. It reads better and costs an amendment to
  a state machine that four documents already enumerate exhaustively.
- **Resuming into a warm in-memory continuation**: rejected. It would make
  resumption depend on the process that suspended still being alive, which
  is exactly the assumption a durable queue exists to remove.
- **A separate reaper process for leases, approvals, and deadlines**:
  rejected in favor of sweeps in every process guarded by advisory locks,
  because a singleton is an operational burden and a single point of
  failure for three time-based obligations. Recorded as an open question;
  the advisory-lock form makes every node do a little wasted work every
  interval.
- **Letting a fenced worker finish its stream so the output can be
  logged**: rejected; it spends tokens and provider capacity on a result
  that is definitionally uncommittable. Recorded as an open question,
  since the diagnostic value is real and the cost is bounded.
- **Deferring this document until after Milestone 1 is implemented**:
  rejected, and it is the alternative with the strongest surface appeal.
  The loop is where every other spec's Milestone 1 deliverable is called
  from, so deferring it means the first implementation invents these
  twenty resolutions under time pressure and the document that follows
  describes whatever was built.
