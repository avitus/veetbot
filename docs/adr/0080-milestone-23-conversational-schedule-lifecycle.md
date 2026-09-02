# ADR-0080: Milestone 23 conversational schedule lifecycle

- Status: Proposed (authorized by the repository owner, 2026-09-02)
- Date: 2026-09-02
- Related: Section 21 of the engineering plan; ADR-0021, ADR-0059,
  ADR-0072, ADR-0073, ADR-0075
- Detailed design: `docs/plan/scheduling.md`, Milestone 23 in
  `docs/plan/engineering-plan.md`

## Context

Milestone 11 implemented principal-scoped schedule list, pause, resume, and
cancel application services and HTTP routes. Milestones 19 and 20 exposed only
`schedule.create` to the model. Consequently an owner could ask the agent to
create a recurring briefing but could not later ask that same agent to pause,
resume, or delete it. The model had no safe way to discover the schedule's
stable identifier and current revision, so pretending to perform the change or
selecting by title alone would have risked changing the wrong schedule.

On 2026-09-02 the owner explicitly authorized conversational pause, resume,
and delete. This is new work rather than a reinterpretation of the deferred
scope in Milestones 19 and 20.

## Decisions

1. **Milestone 23 is an independently advancing parallel workstream.** It does
   not move the verified sequential ceiling past unfinished Milestones 13
   through 15.
2. **Four model tools reuse the existing application service.**
   `schedule.list` discovers bounded, summary-only records;
   `schedule.pause`, `schedule.resume`, and `schedule.cancel` perform the
   existing lifecycle transitions. They make no internal HTTP calls and add no
   persistence path.
3. **Conversational “delete” means terminal cancellation.** The model calls
   `schedule.cancel`; the schedule and occurrence ledger remain for audit, and
   an already materialized run is not cancelled. Hard deletion is not added.
4. **Discovery precedes mutation.** Mutation arguments require a stable
   `schedule_id` and `expected_revision`. Titles are display and matching hints,
   never mutation keys. An ambiguous title requires clarification, and a stale
   revision fails closed.
5. **Existing least-privilege scopes remain exact.** `schedule.list` requires
   `schedule.read`; pause and resume require `schedule.write`; cancel requires
   `schedule.cancel`. The read is `NONE`/`LOW`/`READ_ONLY`; the writes
   are `EXTERNAL_WRITE`/`HIGH`/`IDEMPOTENT`; cancel is
   `EXTERNAL_DELETE`/`HIGH`/`IDEMPOTENT`. Every mutation is non-parallel and
   therefore requires the ordinary approval policy.
6. **The existing schedule deployment pair gates all five schedule tools.** A
   session advertises them only while both `AGENT_SCHEDULE_API_ENABLED` and
   `AGENT_SCHEDULE_WORKER_ENABLED` are enabled.

## Consequences

- A conversation can list schedules, resolve the intended record, present a
  concrete approval, and safely pause, resume, or cancel it.
- Resume retains the Milestone 11 rule: it chooses the first future occurrence
  strictly after the current instant and never backfills paused time.
- Cancel retains schedule/run separation and audit history.
- The model still cannot update schedule content or cadence, inspect occurrence
  history, hard-delete schedule state, or delegate scopes to scheduled runs.
- Seven `gate.schedule.*` entries cover composition, discovery, mutation,
  authorization, validation/conflict behavior, and retry safety.

## Alternatives considered

- **Expose only pause, resume, and delete:** rejected because a model cannot
  safely translate a title such as “the Mon–Fri briefing” into the stable ID
  and revision required by the lifecycle service.
- **Mutate by title:** rejected because titles are neither unique nor stable.
- **Add hard deletion:** rejected because it would erase the durable lifecycle
  and occurrence evidence that cancellation deliberately preserves.
- **Call the authenticated HTTP API from the tool:** rejected because it would
  add credentials and a second authorization boundary inside the process.
