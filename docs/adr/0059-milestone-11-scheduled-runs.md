# ADR-0059: Milestone 11 scheduled runs

- Status: Proposed
- Date: 2026-08-19
- Related: Sections 14, 16, 21, 22, 27, and 29; ADR-0004, ADR-0009,
  ADR-0010, ADR-0023, ADR-0028
- Detailed design: `docs/plan/scheduling.md`

## Context

The platform can execute an on-demand run durably, survive worker crashes,
resume from checkpoints, and expose the result after the submitting client
disconnects. It also has a `runs.scheduled_for` timestamp, but that timestamp is
only queue eligibility and retry backoff. There is no durable recurrence
definition, occurrence identity, civil-time rule, lifecycle API, misfire rule,
or authority refresh at firing time.

Scheduling was listed among Milestone 10's optional extensions while Milestone
10 later acquired independently authorized memory, skill-authoring, and web
workstreams with their own completion contract. Adding a scheduler there would
mix a new trust and time boundary into an already incomplete gate set. The owner
has declared scheduling vital and authorized it as the next logical milestone.

## Proposed decisions

1. **Scheduling is Milestone 11.** Milestone 10 keeps its existing seventeen gates
   and completion rule. Milestone 11 may be developed while Milestone 10 rollout
   evidence remains open, but the verified ceiling advances only in numerical
   order.
2. **A scheduler materializes ordinary runs.** It does not execute model or tool
   work and introduces no second run state machine or queue.
3. **The MVP cadence is closed.** One-time, daily, and weekly rules with IANA
   zones ship first. Arbitrary cron, monthly rules, and DAGs remain future.
4. **Definitions are revisioned and occurrences are immutable.** The unique key
   `(schedule_id, nominal_fire_at)` is the idempotency boundary. Occurrence,
   dedicated session, queued run, seed checkpoint, and audit writes share one
   PostgreSQL transaction.
5. **Civil time has explicit edge semantics.** Ambiguous local time uses the
   earlier instant; nonexistent local time advances to the first valid instant
   that date; recurrence is recomputed from the civil rule rather than by UTC
   duration arithmetic.
6. **Catch-up and overlap are bounded.** One scan creates at most one occurrence
   per schedule, old instants coalesce into one audit record, and an in-flight
   occurrence causes the next to be skipped rather than queued or overlapped.
7. **Authority is fresh at firing.** Schedules store identity and requested
   scopes, never credentials. Materialization resolves a current authority
   version and fails closed if identity, scopes, agent version, policy profile,
   or tenant admission no longer permits the run.
8. **Each occurrence gets a dedicated session.** This preserves the one-active-
   run constraint and bounds history. Governed memory, not session reuse,
   provides cross-occurrence continuity.
9. **Schedule and run cancellation are different authorities.** Cancelling a
   schedule prevents future materialization and never changes an existing run.
10. **PostgreSQL remains the only durable queue.** Scheduled runs use async
    priority 10 and the existing lease, fencing, checkpoint, and recovery path.
    `LISTEN`/`NOTIFY` remains a best-effort wakeup over a bounded poll fallback.
11. **Scheduling is default-off and operationally bounded.** Production enablement
    requires PostgreSQL, a durable principal directory, reserved worker
    capacity, per-tenant concurrency and rate limits, finite per-run limits, and
    daily and monthly cost ceilings.
12. **Offline retrieval is part of the milestone; push delivery is not.** The
    occurrence ledger links to durable run results. Notifications require a
    separate destination and retry contract.
13. **Erased materialized links are explicit.** A materialized occurrence has
    either both live session/run identifiers or neither identifier plus
    `links_erased_at`. Session erasure stamps the marker and clears both foreign
    keys atomically before the existing deletion path runs in the same
    transaction. This keeps
    operational history without representing erased content as a malformed or
    failed occurrence.

## Consequences

- Existing execution safety applies without a parallel implementation.
- `scheduled_for` keeps one meaning: the earliest instant a queued run may be
  claimed. Recurrence and occurrence history live in schedule tables.
- Three exact schedule scopes and four persistence tables are added.
- A current-principal directory becomes a startup requirement for cloud
  scheduling; a stored bearer token is never an acceptable substitute.
- Dedicated sessions trade conversational continuity for bounded, conflict-free
  execution. Long-term memory remains available to every occurrence.
- Extended scheduler downtime cannot cause an unbounded catch-up storm.
- Cancelling recurring work cannot accidentally become permission to interrupt
  an already executing run.
- Session erasure preserves occurrence audit history while a database constraint
  distinguishes governed erasure from missing or partially written links.
- The executable versioned-configuration inventory grows from 106 to 121 with
  four tenant admission ceilings, six finite definition ceilings, three
  positive worker batch and timing limits, and two reserved-capacity limits.
  The environment layer gains two default-off feature flags for the schedule
  API and worker; production activation requires them to change together.

## Alternatives considered

- **Keep scheduling in Milestone 10:** rejected because Milestone 10 already has
  independently delivered tranches and a seventeen-gate completion rule. A new
  gate area belongs in logical succession.
- **Use operating-system cron to call the API:** rejected because it has no
  tenant isolation, occurrence ledger, atomic run creation, current-authority
  check, portable deployment contract, or shared audit transaction.
- **Create all future runs in advance:** rejected because agent configuration,
  authority, cancellation, and recurrence changes would be stale, and an
  unbounded recurrence cannot be materialized finitely.
- **Reuse one session for every occurrence:** rejected because one slow run
  blocks the next under the database's active-run constraint and history grows
  without bound.
- **Store the creator's bearer token:** rejected because it turns durable task
  state into a credential store and bypasses revocation.
- **Replay every missed occurrence:** rejected because an outage becomes an
  unbounded run and cost storm.
- **Add Temporal, Celery, Redis, or another broker:** rejected for the initial
  milestone. PostgreSQL already supplies the atomic boundary and reliable run
  queue; evidence must show it inadequate before another system is introduced.
