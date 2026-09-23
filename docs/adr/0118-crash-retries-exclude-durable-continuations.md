# ADR-0118: Crash retries exclude durable continuations

- Status: Accepted (owner authorized all four incident recommendations, 2026-09-23)
- Date: 2026-09-23
- Related: ADR-0004, ADR-0023; engineering plan Sections 14 and 27
- Detailed design: `docs/plan/event-log-and-persistence.md`

## Context

A production run answered three clarification questions successfully and then
remained queued indefinitely. Each ordinary continuation incremented `attempts`,
and the claim query excluded rows at the three-attempt limit. The lease reaper
could not fail the row because it was queued without a lease. The same limit
also counted approval and child-run continuations as failed worker attempts.

## Decisions

1. Keep `attempts` as the total number of worker claims and `lease_epoch` as the
   fencing identity. Add `lease_expirations`, a nonnegative durable count of
   expired worker executions, incremented only by the locked lease reaper.
   Ordinary input, approval, and child continuations do not change it.
2. Apply the existing `queue.max_attempts` limit to expired executions: with the
   default of three, the first two expirations requeue with one- and two-second
   backoff, and the third fails. Normal continuations neither consume nor reset
   this allowance. Run budgets, deadlines, policy revalidation and effect
   watermarks remain authoritative and unchanged.
3. Extend the bounded reaper to fail queued rows whose expiration allowance is
   exhausted, emitting `run.failed` atomically with the terminal transition.
   This adds the `QUEUED -> FAILED` edge for queue retry exhaustion; a row must
   not be silently excluded from claims forever.
4. Backfill the counter from durable `run.requeued` events and lease-reaper
   `run.failed` events. Do not interpret total claims as crashes or reset real
   recovery history. Existing stranded continuations with unused crash allowance
   become claimable after deployment, retaining their checkpoints, answers,
   tool identities and approval requirements. Terminal runs are never reopened.

## Verification and recovery

PostgreSQL coverage must include three and five clarification rounds followed
by approval and one effect, repeated lease expiration with monotonic fencing,
queued exhaustion with one terminal event, and migration backfill and round
trip. Existing non-idempotent recovery coverage must continue to forbid redial
after a possible effect. Production recovery verifies the saved incident run's
state, counter, events and approvals after deploying the reviewed revision;
it must not inject an approval or create a replacement call.

During this counter migration, stop the legacy maintenance reaper before
backfill and restart it on the new revision. Otherwise an old reaper could
record another expiration after backfill without updating the new column.
The application release already restarts maintenance when it promotes code;
the operator must keep it stopped through migration and preflight, including
any deployment failure, until the matching revision is active.

## Alternatives considered

- Raising the total-claim limit only postpones the same stall.
- Resetting the counter after every suspension loses actual crash history.
- Removing the claim limit without terminal handling strands exhausted work
  elsewhere or retries crashes without a bound.
- Client-only timeout wording cannot make the backend run claimable.
