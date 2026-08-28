# ADR-0073: Milestone 20 calendar recurrence and conversational schedules

- Status: Proposed
- Date: 2026-08-27
- Related: Sections 8, 9, 21, 22, and 29 of the engineering plan;
  ADR-0021, ADR-0024, ADR-0026, ADR-0059, ADR-0062, ADR-0065, ADR-0072
- Detailed design: `docs/plan/scheduling.md`

## Context

Milestone 11 delivered one-time, daily, and weekly schedules through the
authenticated HTTP control plane. Milestone 19 added a model-callable bridge,
but deliberately limited it to one-time creation. The owner has identified
daily, weekly, monthly, and yearly scheduling as core personal-agent behavior
and authorized a more flexible calendar recurrence surface.

Adding monthly recurrence is roadmap item B5 and therefore requires a separate
ADR. Yearly recurrence has the same calendar-edge decisions and belongs in the
same closed extension. The existing revision, occurrence, materialization,
authority, admission, notification, and lifecycle contracts remain suitable;
the missing decisions are the cadence values, their calendar semantics, and
the model-callable input shape.

## Proposed decisions

1. **Calendar recurrence is parallel Milestone 20.** It extends the existing
   schedule area without advancing the sequential verified ceiling past
   unfinished Milestones 13 through 15.
2. **The closed cadence union gains `MONTHLY` and `YEARLY`.** Daily and weekly
   remain unchanged. Monthly rules select one or more numbered days, the last
   day, or both. Yearly rules select one or more unique month/day pairs.
3. **Missing dates are explicit rather than silently clamped.** A numbered
   monthly day that a month lacks is skipped. `last_day = true` is the explicit
   month-end rule. February 29 in a yearly rule fires only in leap years;
   impossible dates such as April 31 are invalid.
4. **Every calendar cadence uses the existing civil-time rules.** It stores an
   IANA zone and whole-second local time, chooses the earlier instant in a fold,
   advances to the first valid instant in a gap, and recomputes from the civil
   rule rather than elapsed UTC duration.
5. **Catch-up stays bounded.** Next and previous calendar occurrences are
   found through bounded month/year candidate searches. Coalesced occurrence
   counts use calendar arithmetic rather than iterating once per missed firing.
6. **`schedule.create` expands compatibly.** The existing top-level `at` input
   remains valid for one-time schedules. A caller may instead supply exactly
   one closed `cadence` object for `DAILY`, `WEEKLY`, `MONTHLY`, or `YEARLY`.
   Supplying both or neither fails validation.
7. **Governance does not change.** Recurring creation remains
   `EXTERNAL_WRITE`/`HIGH`/`CONDITIONALLY_IDEMPOTENT`, non-parallel, approval-
   gated, and exactly scoped by `schedule.write`. Every created revision still
   delegates no tool scopes and derives finite per-occurrence limits.
8. **The existing control plane is reused.** HTTP create and update accept the
   widened cadence union through `ScheduleDefinition`; the tool still calls
   `ScheduleService.create` directly. There is no migration, second scheduler,
   natural-language parser, or alternate persistence path.
9. **Six gates own the extension.** They cover calendar value invariants,
   deterministic calendar recurrence, bounded coalescing, HTTP round trips,
   governed conversational creation across all four recurring kinds, and
   invalid/replayed calls without duplicate state.
10. **The new cadence values create a semantic rollback boundary.** No schema
    migration is needed, but a pre-Milestone-20 binary cannot deserialize a
    persisted `MONTHLY` or `YEARLY` revision. Once either value exists in
    `schedule_revisions.definition`, code-only rollback to a pre-Milestone-20
    release is forbidden. Recovery must roll forward, or restore a database
    snapshot from before the first such revision and then roll the code back.

## Consequences

- Veetbot can represent daily, multi-day weekly, numbered-day or month-end
  monthly, and multi-date yearly schedules in the cloud and create each form
  from conversation after approval.
- Existing stored schedule definitions remain valid and require no migration.
- Alembic-head compatibility alone is insufficient when selecting a rollback
  target across Milestone 20; operators must also check the persisted cadence
  discriminator values.
- A request for "the 31st" skips shorter months; a request for "the last day"
  follows the end of every month. Leap-day yearly schedules remain stable and
  do not drift to February 28 or March 1.
- The occurrence ledger, no-overlap rule, bounded misfires, fresh authority,
  cost admission, dedicated sessions, offline results, and content-free
  schedule-outcome notification remain unchanged.
- Model-callable list, update, pause, resume, and cancel remain out of scope, as
  do arbitrary cron, dependency graphs, workflow DAGs, continuous-session
  recurrence, and delegated tool scopes.

## Alternatives considered

- **Adopt arbitrary cron:** rejected for this milestone because cron does not
  carry portable IANA civil-time semantics for month-end and leap-day intent,
  and it would widen validation far beyond the four requested calendar kinds.
- **Use RFC 5545 as the public model:** rejected for the first extension. It is
  expressive but substantially enlarges the parser and conformance surface;
  the closed values cover the requested personal schedules directly.
- **Clamp every missing date to month-end:** rejected because "the 31st" and
  "the last day" are different user intents. The contract represents them
  separately.
- **Replace `at` with a cadence object:** rejected because the one-time tool is
  already delivered. The additive exactly-one-of shape preserves compatible
  callers and idempotency hashes.
