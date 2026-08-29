---
title: Scheduled Runs
status: design
canonical: true
---

# Scheduled runs

This document specifies Milestones 11, 19, and 20 and the authorized native
schedule browser. The engineering plan states the requirement; this document states the mechanism. It is subordinate to
[engineering-plan.md](engineering-plan.md), and it reuses rather than replaces
the durable run queue, run loop, policy engine, event log, and HTTP boundary.
[ADR-0059](../adr/0059-milestone-11-scheduled-runs.md) records the architectural
decisions for the control plane, ADR-0072 records the original one-time
conversational bridge, ADR-0073 records the calendar-recurrence extension, and
ADR-0075 records the transport-only Apple inspection surface.

The scheduling entry condition is satisfied: PostgreSQL-backed on-demand runs,
leases, fencing, checkpoints, recovery, cancellation, and the public run API
are complete through Milestone 9. Milestone 11 is separately authorized while
Milestone 10 remains in progress. Its gates may become green independently,
but the verified gate ceiling cannot advance past an incomplete earlier
milestone.

## Scope

Milestone 11 delivers a small scheduling control plane that materializes
ordinary durable runs. It includes:

- one-time, daily, and weekly schedules;
- IANA time-zone handling for recurring schedules;
- immutable schedule revisions and an occurrence ledger;
- create, list, read, update, pause, resume, cancel, and occurrence-list HTTP
  operations;
- current-authority resolution when an occurrence fires;
- deterministic misfire, overlap, and daylight-saving-time behavior;
- asynchronous queue priority, per-tenant admission, and cost controls;
- durable audit events, metrics, and offline result retrieval through the
  occurrence-to-run link.

Milestone 11 does not include arbitrary cron expressions, monthly calendar
rules, dependency graphs, workflow DAGs, user-selectable catch-up algorithms,
push notifications, general-purpose subagents, or a second queue technology.
It deliberately did not make a schedule a tool the model may create; schedule
management was an authenticated application surface. Milestone 19, authorized
by the owner on 2026-08-24 and recorded in ADR-0072, adds the narrow
model-callable creation surface specified below without changing the Milestone
11 control plane or its completed gates. Milestone 20, authorized by the owner
on 2026-08-27 and recorded in ADR-0073, adds monthly and yearly calendar rules
and widens conversational creation to daily, weekly, monthly, and yearly
schedules. Arbitrary cron or RFC 5545 input, interval multipliers, dependency
graphs, workflow DAGs, continuous-session recurrence, and model-callable
lifecycle mutation remain outside the closed extension.

The owner authorized a native Apple schedule browser on 2026-08-29. It reuses
the existing Milestone 11 list and point-read routes and therefore adds no
milestone, gate, API route, scope, feature flag, or persistence behavior. Its
client contract is specified below.

## The boundary: a scheduler creates runs; it does not execute them

`scheduled_for` already prevents the run queue from claiming work before a UTC
instant. It is a queue eligibility field, not a recurrence definition. The
Milestone 11 scheduler owns recurrence and creates one ordinary run when an
occurrence becomes due:

```text
schedule revision
      |
      v
schedule worker -- one transaction --> occurrence + session + queued run
                                                   |
                                                   v
                                      existing PostgreSQL run queue
                                                   |
                                                   v
                                      existing durable run worker
```

The created run follows every existing rule. It is leased, checkpointed,
fenced, budgeted, cancellable, recoverable, and audited by the same components
as an on-demand run. The scheduler never calls a model or tool, holds a run
lease, resumes a checkpoint, or decides a policy outcome.

This yields three load-bearing invariants:

1. A committed occurrence either has its committed run, session, and seed
   events or has none of them.
2. A scheduled execution has no weaker authority, policy, budget, deadline, or
   audit record than the equivalent on-demand execution.
3. Queue correctness remains in PostgreSQL. `LISTEN`/`NOTIFY` may reduce
   latency, but losing a notification cannot lose an occurrence or a run.

## Domain model

### Schedule

`Schedule` is the mutable control record:

```python
class ScheduleState(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class Schedule(BaseModel):
    id: UUID
    tenant_id: str
    principal_id: str
    state: ScheduleState
    pause_reason: str | None
    current_revision: int
    next_fire_at: datetime | None
    consecutive_failures: int
    created_at: datetime
    updated_at: datetime
```

`pause_reason` is `user` or `failure_limit` when state is `PAUSED`, and null in
every other state. A one-time schedule becomes `COMPLETED` after its sole
occurrence is materialized or recorded missed. `CANCELLED` is terminal.

### Immutable revision

Every create or update writes a complete `ScheduleRevision`. An occurrence
references the exact revision it used. Updating a schedule never rewrites a
past occurrence or a revision:

```python
class CadenceKind(StrEnum):
    ONCE = "ONCE"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    YEARLY = "YEARLY"


class ScheduleRevision(BaseModel):
    schedule_id: UUID
    revision: int
    title: str
    instruction: str
    agent_id: UUID
    agent_version: str
    policy_profile: str
    requested_scopes: frozenset[str]
    limits: RunLimits
    run_timeout_seconds: int
    cadence: dict[str, object]
    timezone: str | None
    misfire_grace_seconds: int
    max_consecutive_failures: int
    created_by_principal_id: str
    created_at: datetime
```

`instruction` is user-authored input and enters the occurrence session as a
normal `USER` message. It is not system text. It may contain sensitive task
data, so API responses return it only from a point read authorized by
`schedule.read`; list responses return metadata and a bounded preview. It is
never logged. Credentials and authentication tokens are rejected at schedule
validation by the existing secret scanner and are never valid revision data.

`agent_version`, `policy_profile`, and `limits` are pinned. The profile must
equal the profile on the pinned `AgentSpec` at create and update time. The
materializer repeats that check before creating a run, so a missing or
inconsistent version produces a configuration failure rather than silently
changing behavior.

Scheduled runs require finite bounds. `run_timeout_seconds`, `max_steps`,
`max_model_calls`, `max_tool_calls`, and `max_cost` must all be present and
positive. At materialization, `deadline_at` is the materialization time plus
`run_timeout_seconds`; the nominal firing instant remains separately audited.
Tenant configuration supplies ceilings for each field.

### Occurrence

The occurrence ledger says what the scheduler decided. It does not duplicate
the run state machine:

```python
class OccurrenceDisposition(StrEnum):
    MATERIALIZED = "MATERIALIZED"
    MISSED = "MISSED"
    SKIPPED_OVERLAP = "SKIPPED_OVERLAP"
    AUTHORIZATION_FAILED = "AUTHORIZATION_FAILED"
    CONFIGURATION_FAILED = "CONFIGURATION_FAILED"


class ScheduleOccurrence(BaseModel):
    id: UUID
    schedule_id: UUID
    schedule_revision: int
    nominal_fire_at: datetime
    disposition: OccurrenceDisposition
    session_id: UUID | None
    run_id: UUID | None
    reason_code: str | None
    authority_version: str | None
    materialized_at: datetime | None
    links_erased_at: datetime | None
    created_at: datetime
```

`MATERIALIZED` requires `session_id`, `run_id`, `materialized_at`, and the
authority version while `links_erased_at` is null. After governed session
erasure, `MATERIALIZED` instead requires both identifiers to be null and
`links_erased_at` to be present; `materialized_at` and the non-secret authority
version remain as audit facts. Partial erasure states are invalid. Every other
disposition forbids the identifiers and `links_erased_at` and requires a stable
reason code. Run progress and outcome are read by joining the linked run rather
than copied onto the occurrence.

The database enforces `UNIQUE(schedule_id, nominal_fire_at)`. This is the
occurrence idempotency key. It prevents two schedule workers, a retry after an
unknown commit, or a process restart from materializing the same nominal
instant twice.

## Cadence and civil time

All persisted instants are timezone-aware UTC. Recurring definitions also keep
their IANA zone and local civil time because UTC alone cannot preserve “09:00
America/Los_Angeles” across daylight-saving transitions.

The supported cadence payloads are closed:

```text
ONCE   {"at": RFC3339 timestamp with an offset}
DAILY  {"local_time": "HH:MM[:SS]", "timezone": IANA name}
WEEKLY {"local_time": "HH:MM[:SS]", "weekdays": [1..7],
        "timezone": IANA name}
MONTHLY {"local_time": "HH:MM[:SS]", "days_of_month": [1..31],
         "last_day": true|false, "timezone": IANA name}
YEARLY {"local_time": "HH:MM[:SS]",
        "dates": [{"month": 1..12, "day": 1..31}, ...],
        "timezone": IANA name}
```

ISO weekday 1 is Monday and 7 is Sunday. Weekly weekdays are unique and stored
in ascending order. Monthly numbered days are unique and stored in ascending
order; at least one numbered day or `last_day = true` is required. A numbered
day absent from a month is skipped, while `last_day` explicitly selects that
month's actual final day. If both select the same date, it fires once. Yearly
month/day pairs are unique and sorted. February 29 is valid and fires only in
leap years; a pair impossible in every Gregorian year, such as April 31, is
invalid. `zoneinfo` from the Python standard library is the time-zone authority;
the runtime records the installed tzdata version in startup diagnostics.

Civil-time resolution is deterministic:

- An ambiguous local time during a fall-back transition chooses the earlier UTC
  instant and fires once.
- A nonexistent local time during a spring-forward transition advances to the
  first valid local instant on that date and fires once.
- A recurrence is calculated from the declared civil rule, never by adding 24
  hours or seven days to the preceding UTC instant.
- Monthly and yearly lookup enumerates only a bounded set of candidate months
  or years; coalesced counts use Gregorian calendar arithmetic rather than one
  loop per missed occurrence.
- `next_fire_at` is cached state. The immutable cadence plus the last recorded
  nominal instant can reconstruct it, and a repair command may replace a wrong
  cache only after emitting `schedule.next_fire_repaired`.

The recurrence calculator is pure and accepts a `Clock`-supplied reference
instant. Ambient wall-clock reads are forbidden.

## Misfires and downtime

One scheduler scan may materialize at most one occurrence per schedule. The
calculator finds the latest nominal instant at or before `now` without
iterating through an unbounded number of missed dates.

`misfire_grace_seconds` is mandatory, positive, and capped by tenant policy.
For a due schedule:

1. If the latest due nominal instant is within the grace window, that instant
   is the candidate. Older due instants are coalesced and represented by one
   `schedule.misfires_coalesced` event containing only `first_nominal_at`,
   `last_nominal_at`, and `count`.
2. If the latest due instant is outside the grace window, no run is created.
   One `MISSED` occurrence is recorded for that latest instant, the same bounded
   coalescing event records older instants, and `next_fire_at` advances to the
   first future instant.
3. A one-time schedule outside its grace window records `MISSED` and becomes
   `COMPLETED`.

Pausing stops materialization. Resuming computes the first future nominal
instant strictly after the resume time and never backfills time spent paused.
The pause and resume events preserve the previous and new `next_fire_at`.

## Overlap

Milestone 11 has one overlap policy: do not overlap occurrences from the same
schedule. A schedule has an in-flight occurrence when its most recent
`MATERIALIZED` occurrence links to a non-terminal run.

If another occurrence becomes due while one is in flight, no run is created.
The latest due nominal instant receives `SKIPPED_OVERLAP`; older due instants
are coalesced by the same bounded rule, and the next future instant is stored.
The existing run continues unchanged. An overlap is not a cancellation and not
a run failure.

Different schedules may run concurrently, subject to tenant admission and the
async worker pool. A later milestone may add a queue-one overlap policy only if
it also defines queue bounds and cost behavior; Milestone 11 does not.

## Identity, authorization, and policy

A schedule stores identity, never credentials. The creator must hold all three
new exact platform scopes as appropriate:

```text
schedule.read
schedule.write
schedule.cancel
```

`schedule.write` authorizes create, update, pause, and resume.
`schedule.cancel` authorizes the terminal cancel operation. Scope names have no
hierarchy or wildcard behavior, consistent with the existing vocabulary.

Creation and update require `requested_scopes` to be a subset of the caller's
current scopes. Firing is a new submission boundary, so it must not trust the
old snapshot. A `SchedulePrincipalDirectory` resolves the current principal by
`tenant_id` and `principal_id`, returning a monotonic `authority_version`. The
adapter must be backed by authoritative local configuration or the same durable
identity store used for requests; it may not call an external identity provider
while holding the schedule transaction.

Materialization succeeds only when:

- the principal still exists and is enabled;
- every requested scope is still granted;
- the pinned agent version exists;
- the pinned policy profile still matches that agent version;
- tenant schedule, concurrency, and cost admission permits the occurrence.

Failure is closed. The scheduler records `AUTHORIZATION_FAILED` or
`CONFIGURATION_FAILED`, emits an audit event with a stable reason code, creates
no session or run, advances the cadence, and increments the schedule's
consecutive-failure counter. It never silently narrows scopes or substitutes an
agent version, policy profile, or looser budget.

The created run snapshots exactly `requested_scopes`, not every scope the
principal currently holds. It also records `authority_version` and the schedule
and occurrence identifiers in the `run.queued` payload. Raw bearer tokens,
cookies, API keys, and identity-provider assertions appear in no schedule,
revision, occurrence, event, or run field.

## Sessions, context, and results

Each materialized occurrence creates a dedicated session and exactly one run.
This is mandatory rather than policy-selectable in Milestone 11. It avoids the
one-active-run-per-session constraint, prevents an overdue occurrence from
blocking an unrelated occurrence, and bounds conversational history.

The session stores `schedule_id`, `schedule_revision`, `occurrence_id`, and
`nominal_fire_at` in metadata. Its agent ID and version match the revision. The
instruction is appended as the session's first user event and seeds the run in
the same transaction.

Cross-occurrence continuity comes from the principal's governed long-term
memory and knowledge stores, not from reusing a conversation session. Provider
opaque continuation state never crosses occurrences.

`GET /v1/schedules/{id}/occurrences` is the durable offline inbox. A
materialized row embeds the existing run view or links to `GET /v1/runs/{id}`.
Clients need not have been online when the occurrence fired. Push, email, and
mobile notification delivery remain a separate surface because the current
`NotificationService` seam has no delivery contract.

## State transitions

Schedule transitions are closed:

```text
ACTIVE    -> ACTIVE       update creates revision N+1
ACTIVE    -> PAUSED       user pause or failure limit
ACTIVE    -> COMPLETED    one-time occurrence decided
ACTIVE    -> CANCELLED    explicit cancel
PAUSED    -> PAUSED       update creates revision N+1
PAUSED    -> ACTIVE       explicit resume
PAUSED    -> CANCELLED    explicit cancel
COMPLETED -> (none)
CANCELLED -> (none)
```

Every mutation supplies `expected_revision`. A stale value returns a conflict
carrying the current revision and changes nothing. Pause, resume, and cancel are
idempotent when repeated against the already reached state; an incompatible
terminal transition returns a conflict.

Canceling a schedule prevents future materialization. It does not cancel a run
already created. The caller may separately invoke the existing run cancel
endpoint with `run.cancel`; this separation prevents `schedule.cancel` from
becoming indirect authority to cancel arbitrary execution.

After a linked run reaches `COMPLETED`, a post-run hook resets
`consecutive_failures` to zero. `FAILED`, authorization failure, configuration
failure, and a missed occurrence increment it. User-cancelled runs and overlap
skips do not. Reaching `max_consecutive_failures` pauses the schedule with
`pause_reason = failure_limit` and emits `schedule.auto_paused`. The hook is
idempotent on occurrence ID.

## Materialization transaction

`ScheduleWorker.run_once()` performs bounded batches. For each due schedule it
opens one short transaction and:

1. Selects and locks the schedule row with `FOR UPDATE SKIP LOCKED`, requiring
   `state = ACTIVE` and `next_fire_at <= now`.
2. Loads the immutable current revision and calculates the latest due nominal
   instant and first future instant.
3. Resolves current authority and admission from local authoritative state.
4. Checks overlap, grace, configuration, scopes, and budgets.
5. Inserts the unique occurrence disposition.
6. For `MATERIALIZED`, creates the dedicated session, appends `session.created`
   and the user message, creates the priority-10 `QUEUED` run with
   `scheduled_for = now`, seeds its checkpoint, and appends `run.queued`.
7. Advances `next_fire_at`, applies the one-time terminal transition where
   required, and appends schedule audit events.
8. Commits, then sends a best-effort queue notification.

The transaction performs no model, tool, network, object-store, or external
identity-provider I/O. A crash before commit leaves the schedule due and no
partial occurrence. A crash after commit leaves one complete occurrence and
run; a retry encounters the uniqueness constraint and returns the committed
row.

The scheduler sleeps until the earlier of the next known `next_fire_at` and a
bounded fallback poll. Schedule create, update, and resume may send a best-effort
notification to wake it. Correctness depends only on the table scan.

## Capacity, fairness, and cost

Scheduled runs use async priority 10. They never use interactive priority 0.
The existing reserved-capacity rule remains authoritative: an async backlog
must not consume the worker slots reserved for interactive work, and interactive
work must not consume the minimum async capacity that guarantees schedules
eventually progress.

Admission is checked at materialization, before a run is created:

- a configured maximum of active scheduled runs per tenant;
- a configured maximum of materializations per tenant per minute;
- a configured daily and monthly scheduled-cost ceiling;
- the per-run limits pinned on the revision.

Admission denial records no run. A concurrency denial is transient: the
schedule remains due and is retried after a bounded admission backoff without
creating an occurrence. Rate or rolling-cost denial records a missed occurrence
with its stable reason and advances cadence, so a cost ceiling cannot create an
unbounded queue. All counters use committed run usage; reservations charge the
revision's `max_cost` while a scheduled run is active and reconcile on terminal
usage.

## Public API

Milestone 11 adds these routes:

```text
POST   /v1/schedules
GET    /v1/schedules
GET    /v1/schedules/{schedule_id}
PATCH  /v1/schedules/{schedule_id}
POST   /v1/schedules/{schedule_id}/pause
POST   /v1/schedules/{schedule_id}/resume
DELETE /v1/schedules/{schedule_id}
GET    /v1/schedules/{schedule_id}/occurrences
```

`POST /v1/schedules` requires `Idempotency-Key`. Schedule request idempotency is
stored separately from run-submission idempotency under the composite key
`(tenant_id, principal_id, key)`, with a request hash and resulting schedule
ID. Same key and same hash returns the existing schedule; same key and different
hash is a conflict.

`PATCH`, pause, resume, and delete require `expected_revision` and apply the
state rules above. List routes use opaque stable cursors and bounded limits.
Cross-tenant or cross-principal access returns the same not-found envelope as
the existing API. Every route declares its required schedule scope in OpenAPI.

Validation rejects unknown cadence fields, naive datetimes, invalid IANA zones,
duplicate or out-of-range weekdays, duplicate or out-of-range monthly days,
empty monthly selectors, duplicate or impossible yearly dates, empty yearly
selectors, non-positive bounds, a grace or timeout above tenant ceilings,
unknown scopes, mismatched agent policy, and secret-like instructions. Errors
use the existing envelope and stable reason codes under `schedule.*`.

## Native Apple schedule browser

The browser is a presentation of the authenticated principal's existing
schedule control-plane records, not a second schedule service. A calendar entry
beside Memory in the native sidebar opens a list/detail sheet. Presentation
reloads page one from the server; the client holds only a discardable view
cache and never computes a schedule's next occurrence locally.

The list calls `GET /v1/schedules` with a bounded limit and the server's opaque
cursor. It displays every returned lifecycle state rather than hiding terminal
records: ACTIVE, PAUSED, COMPLETED, CANCELLED, or a future unknown value. Each
row contains the title, a text-labeled state, a human-readable cadence summary,
`next_fire_at` when present, and the server-provided `instruction_preview`.
The preview remains bounded and is never promoted to the complete instruction.

Following a row calls `GET /v1/schedules/{schedule_id}`. Only that authorized
point read supplies the complete instruction. Detail displays the server's
current record and revision: state and pause reason, next firing, cadence,
revision number, complete instruction, requested scopes, pinned agent and
policy identifiers, finite execution limits, failure policy, and lifecycle
timestamps. If the schedule is removed or becomes inaccessible between the
list and point reads, the detail shows the ordinary not-found failure and a
retry affordance; it does not reinterpret the result as version skew.

The client models state and cadence kind as raw strings with typed known-case
accessors. Unknown values render by replacing separators with spaces and
capitalizing the result. Known cadence summaries are:

- ONCE: the absolute `at` instant;
- DAILY: the local time and IANA zone;
- WEEKLY: ISO weekday names, local time, and zone;
- MONTHLY: numbered days and explicit last day, local time, and zone;
- YEARLY: month/day selectors, local time, and zone.

List pagination shares the native client's established safeguards: duplicate
schedule IDs are ignored, a repeated cursor terminates paging, stale page
responses cannot overwrite a newer reload, and a later-page failure retains
the already loaded rows with an inline retry. A 404 or 405 from the list route
means schedule browsing is unavailable on that server. The same statuses from
a point read retain their ordinary HTTP meaning.

The surface is read-only. It has no create, update, pause, resume, cancel,
delete, occurrence, or run-history control and therefore needs only the
existing `schedule.read` scope. Swift transport, model, view-model, structure,
and in-process iOS navigation tests are the acceptance evidence under
ADR-0049's native verification contract. This client-only extension adds no
registered Python gate and does not alter the historical Milestone 11 or
Milestone 20 gate counts.

## Events and audit

Schedule lifecycle events are process events because a schedule exists outside
any session:

```text
schedule.created
schedule.updated
schedule.paused
schedule.resumed
schedule.cancelled
schedule.completed
schedule.auto_paused
schedule.occurrence.materialized
schedule.occurrence.missed
schedule.occurrence.skipped_overlap
schedule.occurrence.authorization_failed
schedule.occurrence.configuration_failed
schedule.misfires_coalesced
schedule.next_fire_repaired
```

Every event carries `schedule_id`, revision, tenant, principal, actor, event
time, and the previous and next state where applicable. Occurrence events also
carry occurrence ID and nominal fire time; a materialized event carries session
and run IDs. Events carry no instruction, credential, raw policy content, or
secret-like validation match.

The session and run event logs retain their existing ownership and schema. A
materialized run adds schedule linkage to its creation payload but otherwise
emits the normal sequence.

## Ports

The application layer owns these provider-neutral ports:

```python
class ScheduleRepository(Protocol):
    async def create(self, schedule: Schedule, revision: ScheduleRevision) -> Schedule: ...
    async def get(self, schedule_id: UUID, principal: Principal) -> Schedule: ...
    async def list(self, principal: Principal, page: Page) -> Page[Schedule]: ...
    async def mutate(self, command: ScheduleCommand) -> Schedule: ...
    async def due(self, now: datetime, limit: int) -> list[UUID]: ...
    async def next_fire_at(self) -> datetime | None: ...


class ScheduleOccurrenceRepository(Protocol):
    async def insert(self, occurrence: ScheduleOccurrence) -> ScheduleOccurrence: ...
    async def list(self, schedule_id: UUID, principal: Principal, page: Page) -> Page[ScheduleOccurrence]: ...


class SchedulePrincipalDirectory(Protocol):
    async def current(self, tenant_id: str, principal_id: str) -> AuthoritySnapshot: ...


class ScheduleAdmissionController(Protocol):
    async def check(
        self, tenant_id: str, revision: ScheduleRevision, now: datetime
    ) -> ScheduleAdmissionDecision: ...


class RecurrenceCalculator(Protocol):
    def due(self, revision: ScheduleRevision, now: datetime) -> DueCalculation: ...
```

The concrete PostgreSQL repositories join the existing unit of work so
occurrence, session, run, checkpoint, and schedule writes share one transaction.
The recurrence calculator is pure domain logic and has no I/O. The configured
identity adapter resolves the deployment's authoritative principal and exposes
a content-derived authority version; the same adapter is used by the isolated
production scheduler role, so changed roles or scopes produce a new version
without storing a request credential.

Admission returns one of three closed outcomes. `ALLOW` continues the
transaction, `RETRY` leaves the schedule due and writes no occurrence, and
`REJECT` supplies a stable rate or cost reason that is recorded as `MISSED`.
The allow-all controller is development-only. The PostgreSQL controller takes a
tenant-scoped transaction advisory lock, then computes concurrency, rate,
active maximum-cost reservations, and rolling daily and monthly scheduled cost
from authoritative committed rows. This makes two different schedules racing
for the last tenant slot serialize before either creates a run.

`ScheduleWorker.run_once()` reads one deterministic bounded due-ID batch and
then invokes the materializer separately for each definition, so one corrupt
definition cannot roll back or prevent its siblings. Its loop waits for the
earlier of the repository's next active `next_fire_at` and the fallback poll;
an already-due definition uses the bounded admission backoff. Cancellation is
propagated, while definition and scan failures are logged and isolated. The
sole composition root exposes the in-process worker for development and a
separate least-privilege PostgreSQL builder for production. The production
builder validates token-mode configured identity but neither loads nor receives
the API bearer token or any execution-provider credential.

No in-memory scheduler queue is introduced. In-memory repositories exist only
to run application and property tests; production startup refuses scheduling
without PostgreSQL, a durable principal directory, and a non-development worker
topology.

## Persistence

Milestone 11 adds four tables:

```text
schedules
  id UUID PRIMARY KEY
  tenant_id TEXT NOT NULL
  principal_id TEXT NOT NULL
  state TEXT NOT NULL
  pause_reason TEXT NULL
  current_revision INTEGER NOT NULL
  next_fire_at TIMESTAMPTZ NULL
  consecutive_failures INTEGER NOT NULL DEFAULT 0
  created_at TIMESTAMPTZ NOT NULL
  updated_at TIMESTAMPTZ NOT NULL

schedule_revisions
  schedule_id UUID NOT NULL REFERENCES schedules(id)
  revision INTEGER NOT NULL
  definition JSONB NOT NULL
  created_by_principal_id TEXT NOT NULL
  created_at TIMESTAMPTZ NOT NULL
  PRIMARY KEY (schedule_id, revision)

schedule_occurrences
  id UUID PRIMARY KEY
  schedule_id UUID NOT NULL REFERENCES schedules(id)
  schedule_revision INTEGER NOT NULL
  nominal_fire_at TIMESTAMPTZ NOT NULL
  disposition TEXT NOT NULL
  session_id UUID NULL REFERENCES sessions(id) DEFERRABLE INITIALLY DEFERRED
  run_id UUID NULL REFERENCES runs(id) DEFERRABLE INITIALLY DEFERRED
  reason_code TEXT NULL
  authority_version TEXT NULL
  materialized_at TIMESTAMPTZ NULL
  links_erased_at TIMESTAMPTZ NULL
  created_at TIMESTAMPTZ NOT NULL
  UNIQUE (schedule_id, nominal_fire_at)

schedule_idempotency_keys
  tenant_id TEXT NOT NULL
  principal_id TEXT NOT NULL
  key TEXT NOT NULL
  request_hash TEXT NOT NULL
  schedule_id UUID NOT NULL REFERENCES schedules(id)
  created_at TIMESTAMPTZ NOT NULL
  PRIMARY KEY (tenant_id, principal_id, key)
```

Indexes support `(state, next_fire_at)` for active due scans,
`(tenant_id, principal_id, updated_at, id)` for listing, and
`(schedule_id, nominal_fire_at DESC)` for occurrence history. A partial unique
index on non-null `run_id` makes one durable run link belong to at most one
occurrence. Revision
definitions use the same canonical JSON rules as versioned agent and policy
records. Database constraints enforce state/pause-reason consistency and the
occurrence disposition's nullable-field rules.

The two nullable occurrence-link foreign keys are initially deferred so the
natural-key occurrence can be inserted before its dedicated session and run in
the same materialization transaction. Commit still requires both linked rows,
and the occurrence constraint rejects every non-materialized linked form.

Session erasure follows the existing deletion contract. In the same transaction
and before deleting the session graph, it atomically sets `links_erased_at` and
clears both identifiers on the linked materialized occurrence. The `ON DELETE
SET NULL` foreign keys remain a defensive backstop. The database accepts only
the live-linked or explicitly erased
forms, so a generic nullable link cannot be mistaken for a failed
materialization. The occurrence, schedule identity, nominal and materialized
times, disposition, non-secret authority version, erasure timestamp, and audit
events remain for the configured operational retention period; erased session
or run content does not. The links never cascade from a session into schedule
history.

## Configuration and deployment

Scheduling is default-off through `AGENT_SCHEDULE_API_ENABLED=0` and
`AGENT_SCHEDULE_WORKER_ENABLED=0`. Production release validation accepts only
`0` or `1` and requires the two flags to change together. Enabling it requires:

- PostgreSQL storage;
- the schedule API and schedule worker feature flags;
- a durable principal-directory adapter;
- at least one async worker slot and one interactive reserved slot;
- finite tenant ceilings for timeout, per-run cost, active runs, rate, daily
  cost, and monthly cost;
- a positive scan batch, fallback poll interval, and admission backoff.

The API, interactive worker, async worker, and schedule worker are separate
roles in production. Interactive and async workers claim disjoint configured
priority classes, preserving at least one slot for each workload. Multiple
schedule workers are safe. The schedule worker has database access and no API
bearer token, model-provider, tool, sandbox, or object-store credential because
it executes none of those capabilities. Release validation connects through
that role and verifies the exact table privileges needed to check the schema
head, seed the session-history projection and checkpoint, materialize the run,
and enqueue schedule notifications; it also rejects superuser and `BYPASSRLS`
authority. Create, update, and resume issue a fixed-channel PostgreSQL
notification after commit; the scheduler always keeps its bounded table-scan
fallback.

## Tracked metrics

Track:

- due-to-materialized lag p50, p95, and p99;
- due schedules and scheduler scan duration;
- materialized, missed, overlap-skipped, authorization-failed, and
  configuration-failed occurrence counts;
- coalesced misfire counts and outage span;
- active scheduled runs by tenant;
- schedules auto-paused by failure limit;
- scheduled run duration, terminal outcome, cost, lease reclaim, and
  cancellation latency;
- interactive and async claim latency separately;
- daily and monthly scheduled cost against tenant ceilings.

Metrics contain tenant-safe identifiers or aggregates and never instructions or
credentials.

## Model-callable creation

Milestone 19 closes the missing edge between a conversational request and the
existing scheduling control plane. Milestone 20 widens the same capability to
the four recurring calendar kinds without adding another tool:

```text
name                 schedule.create
kind                 capability
source               builtin
target_kind          in_process
side_effect          external_write
risk                 high
idempotency          conditionally_idempotent
required_scopes      schedule.write
timeout_seconds      15
maximum_output_bytes 4096
allow_parallel       false
output_trust         internal_tool
```

The input schema is closed. `title` and `instruction` are always required, and
exactly one of the compatible one-time `at` field or a recurring `cadence`
object is required:

```json
{
  "title": "Throw the ball for Marzipan",
  "instruction": "Remind me to throw the ball for Marzipan.",
  "at": "2026-08-25T02:00:00+00:00"
}
```

```json
{
  "title": "Month-end review",
  "instruction": "Review the month and summarize unfinished commitments.",
  "cadence": {
    "kind": "MONTHLY",
    "local_time": "18:00:00",
    "days_of_month": [],
    "last_day": true,
    "timezone": "America/Los_Angeles"
  }
}
```

`at` is one timezone-aware ISO 8601 instant. A recurring `cadence` is exactly
one `DAILY`, `WEEKLY`, `MONTHLY`, or `YEARLY` payload from the closed domain
union above. Natural-language parsing is not part of the tool: the model uses
`system.current_time` and, when the date, local time, calendar selector, or
timezone is not known, `conversation.ask_user` before proposing a call.
Supplying both `at` and `cadence`, or neither, fails before schedule state.

The tool is registered and present in the default agent only when both
`AGENT_SCHEDULE_API_ENABLED` and `AGENT_SCHEDULE_WORKER_ENABLED` are true.
`schedule` becomes a build-time builtin domain. The ordinary pipeline checks
the exact `schedule.write` scope and the default policy requires approval for
its `EXTERNAL_WRITE` classification. The approval view contains the concrete
title, instruction, exact instant or complete recurring cadence, and the empty
requested-scope set.

Execution calls `ScheduleService.create` directly with
`ToolExecutionContext.principal` and `ToolExecutionContext.idempotency_key`.
It performs no internal HTTP request and sees no credential. The application
service remains the one validator and persistence path, including request-key
replay. A past or naive instant, invalid calendar selector, or cadence with no
future occurrence is an argument failure and leaves no schedule.

The definition pins the active agent version and its policy profile and always
sets `requested_scopes = frozenset()`. Its step, model-call, and tool-call
limits are the minimum of the active agent's limits and the existing schedule
ceilings. Cost is the minimum of the active agent's finite cost or `1` and the
schedule cost ceiling; run timeout is the lesser of 300 seconds and its
ceiling; misfire grace is the lesser of 3,600 seconds and its ceiling; the
schedule permits one consecutive failure before automatic pause. No model
argument can widen any of these values.

The successful result contains `schedule_id`, `state`, `next_fire_at`, and
whether the application request replayed. Milestone 12's notification behavior
is unchanged: after the occurrence's run is accounted, the outbox emits the
generic content-free `schedule_run_finished` notification. The push does not
contain the title or instruction and can arrive after the nominal instant by
the duration of the scheduled run.

## Build sequence

1. Add the domain values, recurrence calculator, and deterministic civil-time
   property tests. **M11.**
2. Add the four-table migration, ORM models, repositories, constraints, and
   contract tests. **M11.**
3. Add the principal-directory and admission ports with development and
   PostgreSQL-backed adapters. **M11.**
4. Add schedule lifecycle application services and boundary tests. **M11.**
5. Add occurrence materialization in one unit-of-work transaction, beginning
   with the concurrent-worker and crash regressions. **M11.**
6. Add the schedule worker, wakeup, bounded poll fallback, and async-capacity
   configuration. **M11.**
7. Add the eight HTTP routes, exact scopes, idempotency, cursor pagination, and
   OpenAPI assertions. **M11.**
8. Add post-run failure accounting, rolling cost admission, metrics, and
   operational dashboards. **M11.**
9. Run the full non-live suite, PostgreSQL integration and resilience lanes,
   hosted CI, and the required GitHub CodeRabbit loop on one final head.
   **M11.**
10. Add the closed `schedule.create` schema, classification, approval view,
    exact-instant conversion, and application-service adapter. **M19.**
11. Register the tool only with both schedule flags, add it to the enabled
    agent roster on the same condition, and prove scope denial and retry
    idempotency through the ordinary pipeline. **M19.**
12. Run the five Milestone 19 gates, the scheduling and notification
    partitions, the complete non-live suite, PostgreSQL integration, hosted CI,
    and the CodeRabbit loop on the final head. **M19.**
13. Add monthly and yearly domain values, bounded calendar lookup and counting,
    and deterministic civil-time regression and property coverage. **M20.**
14. Widen the HTTP definition union and the compatible `schedule.create`
    schema, approval view, and application-service adapter for all four
    recurring kinds. **M20.**
15. Run the six Milestone 20 gates, every scheduling partition, the complete
    non-live suite, PostgreSQL integration, hosted CI, and the CodeRabbit loop
    on the final head. **M20.**

## Hard gates

1. **A future occurrence never fires early.** Generated one-time, daily, and
   weekly definitions over many zones produce no occurrence whose
   `materialized_at` precedes its nominal UTC instant; a worker scan one
   microsecond before `next_fire_at` writes nothing. Registered as
   `gate.schedule.not_early`, property. **M11.**
2. **Concurrent schedulers materialize once.** Two schedule workers race on one
   due definition. Exactly one occurrence, session, run, seed checkpoint, and
   creation-event sequence commit, and the other worker returns without a
   second effect. Registered as `gate.schedule.materialize_once`, case.
   **M11.**
3. **Occurrence creation is atomic across a crash.** Inject a crash after every
   write in materialization. Before commit, no partial row survives and the
   schedule remains due; after commit, retry returns the one complete linked
   occurrence and run. Registered as `gate.schedule.materialize_atomic`, case.
   **M11.**
4. **Civil-time recurrence is deterministic.** Property cases across the IANA
   corpus, including both DST folds, assert earlier-instant selection for an
   ambiguity, first-valid-instant selection for a gap, one firing per civil
   occurrence, and reconstruction of `next_fire_at`. Registered as
   `gate.schedule.civil_time`, property. **M11.**
5. **Downtime catch-up is bounded.** Advance the clock across thousands of
   nominal instants. One scan creates at most one occurrence and one coalescing
   event, advances directly to the first future instant, and never loops once
   per missed date. Registered as `gate.schedule.misfire_bounded`, case.
   **M11.**
6. **Occurrences from one schedule never overlap.** Hold one linked run
   non-terminal across the next due instant. The next instant is recorded once
   as `SKIPPED_OVERLAP`, no second run exists, and a different schedule can
   still materialize. Registered as `gate.schedule.no_overlap`, case. **M11.**
7. **Firing revalidates current authority.** Create a schedule, then revoke a
   requested scope, disable the principal, and change the authority version in
   separate cases. Each due occurrence fails closed with an audited reason and
   no session or run; restoring authority affects only a later occurrence.
   Registered as `gate.schedule.authority_fresh`, case. **M11.**
8. **Scheduled execution uses exactly its requested scopes.** A creator with a
   superset of scopes schedules a restricted run. The run snapshots only the
   requested subset, an undeclared tool is denied, and cross-tenant or
   cross-principal schedule reads return not found. Registered as
   `gate.schedule.scope_isolated`, case. **M11.**
9. **A revision pins reproducible execution inputs.** Update a schedule after an
   occurrence, then rotate the agent's floating configuration. The first
   occurrence still resolves its original agent version, policy profile,
   instruction, scopes, cadence, and limits; the next uses only the new
   revision. Registered as `gate.schedule.revision_pinned`, case. **M11.**
10. **Lifecycle races are linearized.** Property tests interleave update,
    pause, resume, cancel, and due scans with stale and current revisions. Every
    history is equivalent to one allowed serial order, terminal cancellation
    never reopens, and paused time never backfills. Registered as
    `gate.schedule.lifecycle_linear`, property. **M11.**
11. **Schedule cancellation and run cancellation are separate.** Cancel a
    schedule with an active occurrence. Future occurrences stop, the active run
    remains unchanged, and only a separately authorized run-cancel request can
    cancel it. Registered as `gate.schedule.cancel_separate`, case. **M11.**
12. **Every scheduled run is bounded.** Validation rejects a missing or
    non-positive timeout, step, model-call, tool-call, or cost bound. A valid
    occurrence stamps all limits and its derived deadline onto the run, and the
    ordinary runtime enforces them after crash recovery. Registered as
    `gate.schedule.run_bounded`, case. **M11.**
13. **Scheduled load cannot starve interactive work.** Saturate async scheduled
    capacity while submitting interactive runs. Reserved slots keep interactive
    claim latency within its configured bound and preserve at least one async
    slot so schedules also progress. Registered as
    `gate.schedule.priority_fair`, case. **M11.**
14. **A client can recover every result while offline.** Fire successful,
    failed, cancelled, missed, and overlap-skipped occurrences with no client
    connected. Paginated occurrence reads later return each disposition and the
    exact run link where one exists, with no dependence on transient
    notifications. Registered as `gate.schedule.offline_results`, case.
    **M11.**
15. **No credential becomes schedule state.** A structural walk finds no token,
    secret, cookie, or credential field on schedule domain or persistence
    models; a validation corpus of credential-shaped instructions is rejected
    without storing or logging the matched value. Registered as
    `gate.schedule.no_credentials`, corpus. **M11.**
16. **Schedule values preserve their invariants.** Generated schedule,
    revision, and occurrence inputs either construct a complete legal value or
    fail with a stable validation error; no partially valid state is admitted.
    Registered as `gate.schedule.domain_invariants`, property. **M11.**
17. **Every scheduling port has executable adapter contracts.** The definition,
    occurrence, and request-idempotency protocols each have a named shared
    contract exercised by every registered adapter. Registered as
    `gate.schedule.repository_contract`, structural. **M11.**
18. **The scheduling schema encodes its trust boundaries.** Metadata inspection
    proves primary and foreign keys, occurrence uniqueness, immutable revision
    references, nullable-link rules, erasure evidence, query indexes, and row-
    level security. Registered as `gate.schedule.persistence_schema`,
    structural. **M11.**
19. **Scheduling migrates cleanly from an empty database.** The migration chain
    reaches head and exactly matches the declared SQLAlchemy metadata.
    Registered as `gate.schedule.migration_clean`, case. **M11.**
20. **The scheduling migration is reversible at its boundary.** Upgrade,
    downgrade, and re-upgrade from the immediate predecessor leave a valid
    schema. Registered as `gate.schedule.migration_stepwise`, case. **M11.**
21. **Scheduling repositories share the application transaction.** Definition,
    revision, occurrence, and request-idempotency writes commit or roll back
    together through the ordinary unit of work. Registered as
    `gate.schedule.uow_atomic`, case. **M11.**
22. **Session erasure preserves an explicit occurrence audit state.** Deletion
    atomically marks a materialized link erased and clears the nullable session
    and run links before deleting their graph, without retaining erased content. Registered as
    `gate.schedule.erasure_audited`, case. **M11.**
23. **Scheduling persistence is principal isolated.** PostgreSQL row-level
    security and repository predicates prevent cross-tenant and cross-principal
    reads and mutations. Registered as `gate.schedule.persistence_isolated`,
    case. **M11.**
24. **Conversational creation is a governed, default-off capability.**
    `schedule.create` registers only when both schedule flags are enabled and
    is classified as approval-gated, conditionally idempotent, non-parallel,
    and exactly scoped by `schedule.write`. Registered as
    `gate.schedule.model_create_contract`, structural. **M19.**
25. **A direct reminder request can create one schedule through the ordinary
    tool pipeline.** The call waits for approval, persists one future one-time
    definition, pins the active agent, and delegates no tool scopes. Registered
    as `gate.schedule.model_create_happy_path`, case. **M19.**
26. **Conversational creation cannot outrun its principal.** A run without
    `schedule.write` is denied before execution and leaves no schedule state.
    Registered as `gate.schedule.model_create_authorization`, case. **M19.**
27. **One-time conversational input accepts only an exact future instant.** A
    past or non-timezone-aware `at` instant returns a stable argument failure
    and leaves no schedule state. Registered as
    `gate.schedule.model_create_validation`, case. **M19.**
28. **Conversational creation is retry-safe.** Replaying one tool idempotency
    key and identical normalized arguments returns the original schedule rather
    than creating a duplicate. Registered as
    `gate.schedule.model_create_retry`, case. **M19.**
29. **Calendar cadence values are closed and canonical.** Monthly selectors
    require unique valid numbered days or explicit month-end, yearly selectors
    require unique possible month/day pairs, and every accepted value
    round-trips without semantic drift. Registered as
    `gate.schedule.calendar_values`, property. **M20.**
30. **Monthly and yearly recurrence is deterministic.** Numbered monthly days
    skip missing dates, last-day selects each actual month end, February 29
    skips non-leap years, and both new kinds obey the existing IANA fold, gap,
    and no-early rules. Registered as `gate.schedule.calendar_recurrence`,
    property. **M20.**
31. **Calendar downtime remains bounded.** Next, latest, and inclusive count
    operations cross long month and year spans without iterating once per
    missed occurrence, and one materializer scan still records at most one
    candidate plus one coalescing event. Registered as
    `gate.schedule.calendar_misfire_bounded`, case. **M20.**
32. **The HTTP control plane round-trips every calendar kind.** Create and
    update accept daily, weekly, monthly, and yearly definitions through the
    existing route and immutable revision path, returning the canonical
    cadence and exact next civil instant. Registered as
    `gate.schedule.calendar_http_roundtrip`, case. **M20.**
33. **Conversational recurring creation stays governed and least privilege.**
    Daily, weekly, monthly, and yearly calls use the one `schedule.create`
    capability, wait for ordinary approval, persist one definition, pin the
    active agent, and delegate no scopes. Registered as
    `gate.schedule.model_create_recurring`, case. **M20.**
34. **Conversational calendar validation and replay fail closed.** Both/neither
    `at` and `cadence`, invalid calendar values, and an idempotency key reused
    with different recurrence content create no duplicate state, while an
    identical retry returns the original schedule. Registered as
    `gate.schedule.model_create_recurring_validation`, case. **M20.**

## Open questions

1. Milestone 12 delivered Apple push for schedule outcomes. Email and webhook
   delivery still need their own destination authorization, retry, and
   secret-handling contracts.
2. Arbitrary cron and RFC 5545 recurrence are intentionally outside the closed
   cadence union. Evaluation of real schedule demand should choose which one, if
   either, expands it.
3. Interval multipliers such as every second week require an explicit anchor
   and revision-update phase contract. They remain outside the unanchored
   calendar values rather than acquiring an implicit creation-date phase.
4. A future continuous-session mode would conflict with the one-active-run
   constraint and unbounded conversation growth. It requires separate evidence
   and is not an alternate interpretation of the dedicated-session rule here.
