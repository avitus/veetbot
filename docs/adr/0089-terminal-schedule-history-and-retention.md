# ADR-0089: Recent terminal schedule history and bounded retention

- Status: Proposed (authorized by the repository owner, 2026-09-10)
- Date: 2026-09-10
- Related: Sections 16, 21, and 29 of the engineering plan; ADR-0023,
  ADR-0031, ADR-0049, ADR-0059, ADR-0075, ADR-0080
- Detailed design: `docs/plan/scheduling.md`

## Context

The native schedule browser currently mixes ACTIVE, PAUSED, COMPLETED, and
CANCELLED records into one list. Terminal schedules are useful immediately
after execution or cancellation, but they obscure actionable schedules when
shown forever. Each retained immutable revision also contains the complete
instruction, so unbounded retention has a privacy and storage cost.

The owner asked that recent terminal history remain user-accessible, that old
terminal state be flushed, and that ordinary cadence rows omit a redundant time
zone. This changes ADR-0075's definition of “current schedules” and qualifies
ADR-0080's audit-retention rationale, so it requires an explicit decision.

## Decisions

1. **The browser separates current state from recent history.** Its default
   Current section requests ACTIVE and PAUSED schedules. A user-selected Recent
   History section requests COMPLETED and CANCELLED schedules. Both remain
   read-only and use the existing list and point-read routes.
2. **Lifecycle filtering occurs before pagination on the server.** The existing
   `GET /v1/schedules` accepts a repeated optional `state` query parameter.
   Omission retains the original all-state API behavior used by existing
   callers; supplied states constrain the repository query before its stable
   cursor and limit are applied.
3. **Terminal schedule state is retained for thirty days.** The retention clock
   begins at the terminal transition's `updated_at`. Once the record is at or
   beyond the configured cutoff, the maintenance role deletes the schedule,
   its immutable revisions, occurrence ledger, and schedule-creation
   idempotency rows in one bounded transaction. The linked durable sessions,
   runs, and content-free process events remain under their own retention
   contracts.
4. **Retention is maintenance policy, not a new user or model deletion
   capability.** The shipped configuration uses a 30-day window, an hourly
   sweep, and batches of 100. A transaction advisory lock and row locking make
   concurrent maintenance workers safe. Conversational delete still means
   terminal cancellation; no native lifecycle control or hard-delete tool is
   added.
5. **Time-zone semantics remain authoritative but presentation is
   conditional.** Recurring definitions continue to store and return an IANA
   zone for civil-time and daylight-saving correctness. The Apple client omits
   that zone from cadence summary text when it matches the device's current
   zone and includes it when it differs.

## Consequences

- Actionable schedules are the default view without losing the owner's ability
  to inspect a recent completion or cancellation.
- Offline schedule and occurrence inspection is bounded by the terminal
  retention window; linked run and session history can outlive the schedule
  graph under their independent policies.
- A point read can legitimately become not-found after a list row ages through
  the retention boundary. The client's existing retry/error behavior remains
  correct.
- The new partial tenant/time PostgreSQL index bounds the oldest-terminal selection without
  adding an index to active scheduling writes.
- ADR-0075 decision 3 is superseded. ADR-0080 decision 3 continues to govern
  cancellation itself, while “remain for audit” now means remain during this
  configured retention window rather than indefinitely.

## Alternatives considered

- **Hide terminal records entirely:** rejected because completion and
  cancellation are useful recent history and troubleshooting context.
- **Keep one mixed list:** rejected because inactive records dominate the
  surface as history accumulates.
- **Retain terminal schedules indefinitely:** rejected because the revision
  graph contains full user instructions and has no permanent-record
  requirement.
- **Delete linked runs and sessions with the schedule:** rejected because those
  are authoritative durable-run records with independent lifecycle contracts.
- **Remove time zones from schedule definitions:** rejected because recurring
  civil-time and daylight-saving behavior depends on the stored IANA zone.
