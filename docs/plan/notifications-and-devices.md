---
title: Notifications and Devices
status: design
canonical: true
---

# Notifications and device identity

This document specifies Milestone 12. The engineering plan states the
requirement; this document states the mechanism. It is subordinate to
[engineering-plan.md](engineering-plan.md), and it reuses rather than replaces
the durable run loop, the event log, the scheduling control plane, the HTTP
boundary, and the native Apple client.
[ADR-0062](../adr/0062-milestone-12-notifications-and-device-identity.md)
records the architectural decisions; ADR-0061 records the authorization.

Section 29.8 of the plan deferred the `Device` concept and notifications "to
a milestone with concrete use cases". The use cases now exist: a scheduled
occurrence finishes and nobody is watching; an approval waits and the run is
parked; `ask_user` suspends a run and the question sits unread until the next
time the owner opens the app. [multi-device-and-surfaces.md](multi-device-and-surfaces.md)
audited the seam and found `NotificationService` to be "a port name with
nothing behind it" (multi-device-and-surfaces.md:221). This document puts a
mechanism behind it, lands the `Device` table the audit placed
(multi-device-and-surfaces.md:332), and stops there: no inbound channel, no
pairing of untrusted senders, no device-scoped tool, no presence-based routing.
Those are Milestone 14 and the roadmap.

Milestone 12 is authorized while Milestones 10 and 11 await hosted review. Its
gates may become green independently, but the verified gate ceiling advances
only in numerical order.

## Scope

Milestone 12 delivers the first half of Section 29: a device registry and a
durable, content-free notification path to one transport. It includes:

- a `Device` registry: register, refresh, list, revoke, and delete a device for
  a principal, keyed by a client-minted installation identity;
- a durable notification outbox written in the same transaction as the event
  that triggers it;
- a least-privilege dispatcher role that drains the outbox and holds the push
  credential and the database credential and nothing else;
- one push transport, Apple Push Notification service (APNs), behind a port a
  fake transport satisfies under the same contract;
- exactly five trigger transitions: an approval is requested, a run waits for
  user input, a run fails, a scheduled occurrence's run reaches a terminal
  state, and a scheduled occurrence is missed or skipped;
- content-free payloads that deep-link the Apple client to the session, run,
  approval, or question;
- device and notification routes with exact scopes, an offline notification
  inbox, default-off activation, and the Apple client's registration, token
  upload, revocation, and deep-link handling.

The milestone does not include inbound Surfaces, the session-key resolver, or
pairing (Milestone 14); `DeviceChannel`, device-scoped tools, or the `device.`
tool domain; presence or presence-based tool exposure; per-device scope
narrowing; the hand-off suspension kind; actionable approve-or-deny buttons on
the lock screen; email or webhook transports; or notifications for an
interactive `run.completed`. Each of those is named in the plan's roadmap or
in Milestone 14, and the ports below are shaped so each is additive.

## The boundary: the outbox is written with the event; the transport never is

A notification is a consequence of a state transition the platform already
records. The only reliable place to decide that a notification is owed is the
transaction that records the transition, and the only reliable way to deliver
it is from a durable row, later, by a process that can retry. Nothing in the
interactive request path talks to Apple:

```text
terminal writer / scheduler -- one transaction --> event + outbox row
                                                         |
                                                         v
                                          notify role claims the row
                                                         |
                                                         v
                                     PushTransport.deliver(target, message)
                                                         |
                                                         v
                                      delivery ledger + settled outbox row
```

This yields three load-bearing invariants:

1. A committed triggering event either has its committed outbox row or the
   enqueue was refused inside a savepoint and the run's terminal state is
   unchanged; there is no third state.
2. Delivery is at-least-once from durable rows and idempotent by a
   derivation-style deduplication key; the transport is never a correctness
   dependency and losing a push cannot lose the underlying approval, question,
   or result, all of which remain readable through the existing API.
3. A payload is content-free. It carries identifiers, a closed kind, a closed
   status, and a templated title. The client fetches the content after the
   tap, authenticated, from the API.

The existing `LiveEventBroadcaster` (`ports/live_events.py`) remains the
answer for a client that holds an open connection. The seam audit drew the
distinction this document keeps: delivering to an open connection and
delivering to a device that has none "are different problems with different
durability requirements" (multi-device-and-surfaces.md:221-242). The
broadcaster stays best-effort and in-process; the outbox is durable and
cross-process.

## Domain model

### Device

`Device` is the registry record Section 29.6 names, reduced to what Milestone
12 consumes. The `capabilities` and `granted_scopes` members of 29.6's model
are not columns yet: nothing reads them until a device channel exists, and a
column nothing reads is speculative.

```python
class DeviceKind(StrEnum):
    MOBILE = "mobile"
    LAPTOP = "laptop"
    DESKTOP = "desktop"
    WEB = "web"
    CLI = "cli"


class PushProvider(StrEnum):
    APNS = "apns"


class PushEnvironment(StrEnum):
    SANDBOX = "sandbox"
    PRODUCTION = "production"


class DeviceStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class Device(BaseModel):
    id: UUID
    tenant_id: str
    principal_id: str
    client_device_id: str
    name: str
    kind: DeviceKind
    platform: str
    app_bundle_id: str | None
    push_provider: PushProvider | None
    push_token: SecretStr | None
    push_environment: PushEnvironment | None
    push_token_updated_at: datetime | None
    push_token_invalidated_at: datetime | None
    muted_kinds: frozenset[NotificationKind]
    status: DeviceStatus
    revoked_at: datetime | None
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime
```

`client_device_id` is minted once by the client, persisted in the client's
keychain beside the bearer token, and is stable across token rotation and app
updates; it is unique per principal. `push_token`, `push_provider`, and
`push_environment` are present together or absent together. The token is a
secret value: it is returned to no client, appears in no event or log, and
device views carry a six-character fingerprint of it. `muted_kinds` is the
per-device preference: a phone can be loud and a laptop quiet without a third
table. `status = revoked` requires `revoked_at` and a null token.

### Notification and delivery

The outbox row says what the platform decided to tell a principal. The
delivery ledger says what the transport did about it:

```python
class NotificationKind(StrEnum):
    APPROVAL_REQUESTED = "approval_requested"
    QUESTION_ASKED = "question_asked"
    RUN_FAILED = "run_failed"
    SCHEDULE_RUN_FINISHED = "schedule_run_finished"
    SCHEDULE_OCCURRENCE_SKIPPED = "schedule_occurrence_skipped"
    OPS_ALERT = "ops_alert"                  # Milestone 15 health check
    OPS_RECOVERED = "ops_recovered"          # Milestone 15 health check
    TEST = "test"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    FAILED = "failed"


class Notification(BaseModel):
    id: UUID
    tenant_id: str
    principal_id: str
    kind: NotificationKind
    dedupe_key: str
    session_id: UUID | None
    run_id: UUID | None
    approval_id: UUID | None
    question_id: UUID | None
    schedule_id: UUID | None
    occurrence_id: UUID | None
    payload: NotificationPayload
    priority: int
    expires_at: datetime | None
    status: NotificationStatus
    attempts: int
    next_attempt_at: datetime
    claimed_by: str | None
    claimed_until: datetime | None
    created_at: datetime
    settled_at: datetime | None


class DeliveryOutcome(StrEnum):
    DELIVERED = "delivered"
    RETRY = "retry"
    UNREGISTERED = "unregistered"
    REJECTED = "rejected"
    SKIPPED = "skipped"


class NotificationDelivery(BaseModel):
    id: UUID
    notification_id: UUID
    device_id: UUID
    attempt: int
    outcome: DeliveryOutcome
    provider_reason: str | None
    provider_id: str | None
    attempted_at: datetime
```

`dedupe_key` is the idempotency boundary, in the same form as the
`derivation_key` the event log and the scheduling accountant already use:

```text
approval.requested:{approval_id}
run.waiting_for_user:{run_id}:{question_id}
run.failed:{run_id}
schedule.run_accounted:{occurrence_id}
schedule.occurrence.skipped:{occurrence_id}
device.test:{device_id}:{idempotency_key}
```

The database enforces `UNIQUE(dedupe_key)`. A retry after an unknown commit,
a repeated hook invocation, or a second process recording the same transition
inserts nothing. The identifier columns carry no foreign keys: a notification
about a deleted session is settled `superseded` at dispatch time rather than
being torn out of the ledger, and the ledger is what the offline inbox reads.

### The payload is closed

```python
class NotificationPayload(BaseModel):
    version: Literal[1]
    kind: NotificationKind
    title: str
    status: str | None
    tool_name: str | None
    session_id: UUID | None
    run_id: UUID | None
    approval_id: UUID | None
    question_id: UUID | None
    schedule_id: UUID | None
    occurrence_id: UUID | None
    notification_id: UUID
    signal: str | None            # ops kinds only: a declared health signal
    severity: str | None          # ops kinds only: warn | critical | recovered
    reason_code: str | None       # ops kinds only: from the checked-in table
    release_id: str | None        # ops kinds only
```

`title` is chosen from a fixed template per kind — "Approval needed", "The
agent has a question", "Run failed", "Scheduled run finished", "Scheduled run skipped", "Production alert", "Production recovered", "Test
notification" — and `status` is a closed-enum disposition or run status. The
four ops fields are null for every kind but the two ops kinds, for which
`signal` is one of the declared health signals, `severity` is closed, and
`reason_code` comes from the checked-in table — never free text. `tool_name` is registry vocabulary (`domain.verb`), never user
content. The model has no extra fields. What the payload never carries:
message text, tool arguments, an approval's `action_summary`, question text, a
failure `message`, a schedule's `title` or `instruction`, reasoning, or a
traceback. The reasons are the ones the corpus already states for the event
stream and the export (ADR-0006, ADR-0032, [scheduling.md](scheduling.md)'s
"never logged" rule for instructions), plus one more: this payload transits
Apple's servers and the lock screen of a phone that may be face-up on a table.
The client fetches details after the tap, when it is online and authenticated.

## Triggers

Exactly five transitions enqueue, and each is observed where the corpus
already records it:

```text
trigger                                   observed in                            kind
----------------------------------------  -------------------------------------  ---------------------------
approval.requested                        terminal writer, same transaction as   approval_requested
                                          run.waiting_for_approval
run.waiting_for_user                      terminal writer                        question_asked
run.failed                                terminal writer                        run_failed
schedule.run_accounted                    scheduling accountant, same            schedule_run_finished
                                          transaction as the accounting event
schedule.occurrence.{missed,              scheduling materializer, same          schedule_occurrence_skipped
  skipped_overlap,                        transaction as the occurrence
  authorization_failed,
  configuration_failed}
```

The single terminal writer in `runtime/executor.py` is the only code that
appends `run.failed`, `run.waiting_for_user`, `run.waiting_for_approval`, and
`approval.requested`, and it does so inside one unit of work. The enqueue is an
injected in-transaction callable on the executor, in the shape the checkpoint
seeder already takes, invoked after the event append and wrapped in a
savepoint: an enqueue failure rolls back to the savepoint, is recorded in
the same transaction as a content-free `notification.enqueue_failed` process
event carrying only identifiers and a reason code, and the terminal
transaction commits unchanged. This is the rule ADR-0051 set for the
memory-formation hook and the same reason — a notification must never be able
to fail a run. The only committed triggering event without an outbox row is
therefore one whose enqueue failure is itself audited; there is no silent
form. The scheduling accountant and materializer own their own
transactions and take the outbox through the schedule unit of work.

Interactive `run.completed`, `run.cancelled`, tool lifecycle events, and
message deltas enqueue nothing. A principal who wants to know that an
interactive run finished is, by construction, watching it. A surface reply
(Milestone 14) is a separate outbox class, not a notification. The one
producer that is not a run or schedule transition is Milestone 15's host
health check, which enqueues only the `ops_alert` and `ops_recovered` kinds
through the outbox port with its own deduplication keys (`ops.<signal>`) and
cool-down; it adds no trigger to the five above.

## Dispatch

The `notify` role runs `NotificationDispatcher.run_once()` in bounded batches.
For each claimed row it:

1. Claims pending rows whose `next_attempt_at` is due with
   `FOR UPDATE SKIP LOCKED`, stamping `claimed_by` and a lease `claimed_until`,
   in the manner of the schedule worker's `lock_due`.
2. Checks staleness against the row's identifiers: an approval already
   resolved or expired, a question already answered, a run no longer waiting,
   or a session that no longer exists settles the row `superseded` with no
   transport call.
3. Checks `expires_at`; an expired row settles `expired` with no transport
   call.
4. Resolves push targets: the principal's active devices with a token whose
   `muted_kinds` excludes the row's kind.
5. Calls `PushTransport.deliver` once per target and writes one
   `NotificationDelivery` row per attempt.
6. Settles the row: `dispatched` when at least one target accepted or no target
   exists; `pending` with the next backoff instant when every target returned
   `RETRY`; `failed` when the attempt ceiling is reached.

The transport's outcome vocabulary is closed and mapped deliberately. A
transport 5xx, 429, network failure, or expired provider token is `RETRY` — the
provider token is re-minted and the device is not charged. A 410 `Unregistered`
or 400 `BadDeviceToken` / `DeviceTokenNotForTopic` is `UNREGISTERED`: the
device's token is nulled, `push_token_invalidated_at` is set, a
`device.push_token_invalidated` process event is appended, the delivery is
recorded, and the row is not retried to that device. Any other 4xx is
`REJECTED`: payloads are closed-shape, so a rejected payload is a defect to
surface, not a transient to retry.

Retry follows a closed schedule in the versioned limits file: the attempt
after a `RETRY` happens at +30 seconds, +2 minutes, +10 minutes, +1 hour, and
then the row is `failed`. `expires_at` is the approval's own expiry where one
exists and twenty-four hours for terminal notices, checked before every send.
Two dispatchers are safe under the claim lease, and the per-attempt delivery
ledger records every send; delivery is nonetheless at-least-once, not
exactly-once. If the transport accepts a push and the dispatcher stops before
it writes the delivery row, the next claimant cannot distinguish that accepted
send from no send and re-sends. The transport's collapse identifier (the
`dedupe_key`) makes that replay invisible on the device as a best-effort
reduction, not a guarantee, and the ledger shows the extra attempt.

Wake-up is `LISTEN`/`NOTIFY` on a fixed channel after the enqueuing
transaction commits, over a bounded poll, exactly as the schedule worker does.
Correctness is the table scan.

Revocation is immediate in the only sense that matters here: the dispatcher
resolves targets at claim time, so a device revoked before the next claim
receives nothing. Section 29.7's "immediately removes its scopes and presence
server-side" has no scopes or presence to remove in Milestone 12; the push
target is what is removed.

## The APNs transport

The first and only transport in Milestone 12 is Apple Push Notification
service, because the only client that exists beyond the CLI is the native
Apple app ([apple-client.md](../apple-client.md), ADR-0049). The adapter:

- speaks HTTP/2 to `api.push.apple.com:443` or
  `api.sandbox.push.apple.com:443`, choosing the host per device from
  `push_environment`, never globally — the common production defect is a
  sandbox token sent to the production host, and it surfaces as
  `BadDeviceToken`;
- authenticates with a provider token: an ES256 JWT signed by the `.p8` key
  read from `APNS_KEY_FILE`, carrying the key identifier and team identifier,
  re-minted inside Apple's twenty-to-sixty-minute window; the key is a
  private-file credential in the pattern the browser control plane
  established — a regular file, mode `0600`, never an environment value, never
  logged;
- sends `apns-topic` equal to the configured bundle identifier,
  `apns-push-type: alert`, `apns-priority` 10 for approvals and questions and 5
  for terminal notices, `apns-collapse-id` equal to the `dedupe_key`, and
  `apns-expiration` equal to the row's `expires_at` where present;
- maps every response into the closed `DeliveryOutcome` vocabulary above and
  records the provider's reason string and identifier on the delivery row;
- holds one connection per environment and reuses it.

HTTP/2 needs the `h2` extra of the HTTP client the repository already depends
on; it is the one new dependency, and ADR-0062 records it. The contract suite
runs the adapter against a fake server through the client's mock transport;
the in-memory `PushTransport` serves every application and gate test; real
delivery is verified through the test-notification route against a physical
device, because the simulator cannot receive a remote push.

## Ports

The application layer owns three provider-neutral ports. They live in two new
modules, `ports/devices.py` and `ports/notifications.py`, because the seam
audit's placement rule is that a port lives in the module named for the
capability it abstracts and that a port with no neighbours "needs a new module
rather than a new Protocol in an existing one" (multi-device-and-surfaces.md:332-367):

```python
class DeviceRegistry(Protocol):
    async def upsert(self, device: Device, principal: Principal) -> Device: ...
    async def get(self, device_id: UUID, principal: Principal) -> Device: ...
    async def list(self, principal: Principal, page: Page) -> Page[Device]: ...
    async def revoke(self, device_id: UUID, principal: Principal, at: datetime) -> Device: ...
    async def delete(self, device_id: UUID, principal: Principal) -> None: ...
    async def invalidate_push_token(self, device_id: UUID, reason: str, at: datetime) -> Device | None: ...
    async def push_targets(self, tenant_id: str, principal_id: str, kind: NotificationKind) -> list[PushTarget]: ...


class NotificationOutbox(Protocol):
    async def enqueue(self, notification: NewNotification) -> Notification | None: ...
    async def claim_due(self, now: datetime, limit: int, claimant: str, lease_seconds: float) -> list[Notification]: ...
    async def record_delivery(self, delivery: NotificationDelivery) -> None: ...
    async def settle(self, notification_id: UUID, status: NotificationStatus, next_attempt_at: datetime | None) -> None: ...
    async def list(self, principal: Principal, page: Page) -> Page[Notification]: ...


class PushTransport(Protocol):
    async def deliver(self, target: PushTarget, message: PushMessage) -> PushOutcome: ...
```

`enqueue` returns `None` when the deduplication key already exists; it is an
`INSERT ... ON CONFLICT DO NOTHING`, not a read-then-write. Every port has an
in-memory adapter for application and property tests and a PostgreSQL adapter
for production, and every port has a named shared contract exercised by every
registered adapter — the contract-coverage gate the structural checks already
enforce for every `Protocol` under `ports/`. The outbox and registry
repositories join the existing units of work so the run and schedule
transactions can write them. The in-memory outbox claims no durability across
processes and production startup refuses notification dispatch without
PostgreSQL.

The name `NotificationService` is retired rather than given a body. The seam
audit's open question four asked whether it is one port or two
(multi-device-and-surfaces.md:441); the answer is two, and the broadcaster is
a third thing that already exists.

## Persistence

Milestone 12 adds three tables:

```text
devices
  id UUID PRIMARY KEY
  tenant_id TEXT NOT NULL
  principal_id TEXT NOT NULL
  client_device_id TEXT NOT NULL
  name TEXT NOT NULL
  kind TEXT NOT NULL
  platform TEXT NOT NULL
  app_bundle_id TEXT NULL
  push_provider TEXT NULL
  push_token TEXT NULL
  push_environment TEXT NULL
  push_token_updated_at TIMESTAMPTZ NULL
  push_token_invalidated_at TIMESTAMPTZ NULL
  muted_kinds JSONB NOT NULL DEFAULT '[]'
  status TEXT NOT NULL
  revoked_at TIMESTAMPTZ NULL
  last_seen_at TIMESTAMPTZ NOT NULL
  created_at TIMESTAMPTZ NOT NULL
  updated_at TIMESTAMPTZ NOT NULL
  UNIQUE (tenant_id, principal_id, client_device_id)

notification_outbox
  id UUID PRIMARY KEY
  tenant_id TEXT NOT NULL
  principal_id TEXT NOT NULL
  kind TEXT NOT NULL
  dedupe_key TEXT NOT NULL UNIQUE
  session_id UUID NULL
  run_id UUID NULL
  approval_id UUID NULL
  question_id UUID NULL
  schedule_id UUID NULL
  occurrence_id UUID NULL
  payload JSONB NOT NULL
  priority SMALLINT NOT NULL
  expires_at TIMESTAMPTZ NULL
  status TEXT NOT NULL
  attempts INTEGER NOT NULL DEFAULT 0
  next_attempt_at TIMESTAMPTZ NOT NULL
  claimed_by TEXT NULL
  claimed_until TIMESTAMPTZ NULL
  created_at TIMESTAMPTZ NOT NULL
  settled_at TIMESTAMPTZ NULL

notification_deliveries
  id UUID PRIMARY KEY
  notification_id UUID NOT NULL REFERENCES notification_outbox(id) ON DELETE RESTRICT
  device_id UUID NOT NULL REFERENCES devices(id) ON DELETE RESTRICT
  attempt INTEGER NOT NULL
  outcome TEXT NOT NULL
  provider_reason TEXT NULL
  provider_id TEXT NULL
  attempted_at TIMESTAMPTZ NOT NULL
  UNIQUE (notification_id, device_id, attempt)
```

Check constraints enforce that `push_provider`, `push_token`, and
`push_environment` are null together or present together, that `status =
revoked` implies `revoked_at` and a null token, and that `muted_kinds` holds
only declared kinds. A partial unique index on `(push_provider, push_token)
WHERE push_token IS NOT NULL AND status = 'active'` makes one live token belong
to at most one active device; a token re-registered from a fresh installation
moves, and the old row's token is nulled. Indexes support `(status,
next_attempt_at)` for the dispatcher's due scan and `(tenant_id, principal_id,
created_at, id)` for the inbox and device listings. All three tables carry the
tenant row-level-security policy the Milestone 11 migration established, the
delivery table through its parent.

The migration follows the Milestone 11 schedule head in the linear chain, and
the startup revision assertion moves with it. The identifier columns on the
outbox are deliberately unconstrained by foreign keys: a notification about a
purged conversation must survive long enough to be settled `superseded`, and
the offline inbox must still show that it was enqueued. Session erasure, in the
same transaction and before deleting the session graph, deletes the session's
*pending* outbox rows so that a purged conversation cannot still ring a phone;
settled rows remain as content-free audit facts under the existing retention
rule.

Device lifecycle is audited as process events — `device.registered`,
`device.push_token_updated`, `device.revoked`, `device.push_token_invalidated`,
`device.deleted` — through the existing process-event repository, because a
device exists outside any session. This is the precedent [scheduling.md](scheduling.md)
set for schedule lifecycle and the second of the two ways out the seam audit
named for the `events.session_id NOT NULL` constraint
(multi-device-and-surfaces.md:147-173). The audit's counter-argument, that
audit is split across two logs, does not bite: a device has no session, so
nothing about it was ever going to be in the session log. Payloads carry
identifiers and a token fingerprint, never the token.

## Public API

Milestone 12 adds these routes, mounted on a feature-flagged router exactly
as the schedule routes are:

```text
POST   /v1/devices                                  device.write
GET    /v1/devices                                  device.read
GET    /v1/devices/{device_id}                      device.read
POST   /v1/devices/{device_id}/revoke               device.write
DELETE /v1/devices/{device_id}                      device.write
POST   /v1/devices/{device_id}/test-notification    device.write
GET    /v1/notifications                            notification.read
```

`POST /v1/devices` registers or refreshes: the same `client_device_id` for the
same principal updates the row in place and returns it; `Idempotency-Key` is
accepted and scoped as the schedule routes scope it. Device views return a
token fingerprint, never the token. The test-notification route enqueues a
`test` kind addressed to one device; it is the setup-verification path and the
end-to-end evidence that a real phone received a real push. `GET
/v1/notifications` is the durable offline inbox: every enqueued notification
for the principal with its status and delivery outcomes, paginated with the
existing opaque cursors. Cross-tenant and cross-principal access returns the
existing not-found envelope.

Three scopes extend the closed vocabulary in the form
[http-api-and-streaming.md](http-api-and-streaming.md#the-scope-vocabulary-is-closed-and-matched-exactly)
and [policy-and-approvals.md](policy-and-approvals.md) state:

```text
device.read        device.write        notification.read
```

They are resource-action pairs governing application routes, in the same
coexistence the corpus already has between the `browser.*` tool domain and the
`browser.profile.*` scopes; ADR-0034's objection was to a `device.` scope
namespace for scopes *granted to* a device, which this is not. Validation
rejects unknown kinds, a token without an environment, an environment without
a token, an unknown provider, and muted kinds outside the closed set, with
stable reason codes under `device.*` and `notification.*`.

## Events and audit

Device lifecycle events are process events:

```text
device.registered
device.push_token_updated
device.revoked
device.push_token_invalidated
device.deleted
```

Notification dispatch is audited by the delivery ledger rather than by events;
the ledger is queryable through the inbox and carries the provider's reason
and identifier per attempt. The session and run event logs are unchanged: the
outbox row is a sibling write in the triggering transaction, not a new event
type.

## Configuration and deployment

Notifications are default-off through `AGENT_NOTIFICATION_API_ENABLED=0` and
`AGENT_NOTIFICATION_DISPATCH_ENABLED=0`. The first mounts the routes; the
second enables both the in-transaction enqueue and the dispatcher role.
Production release validation accepts only `0` or `1` and requires the two
flags to change together, as it does for the schedule flags. Enabling dispatch
requires:

- PostgreSQL storage;
- `PUSH_PROVIDER=apns` (the selector form `WEB_SEARCH_PROVIDER` and
  `BROWSER_PROVIDER` use; `disabled` is the default);
- `APNS_KEY_FILE`, `APNS_KEY_ID`, `APNS_TEAM_ID`, and `APNS_TOPIC`, validated
  at startup — the key file must be a regular file readable only by its owner;
- a positive claim batch, lease, poll interval, and the retry schedule and
  expiry defaults in the versioned limits file.

The dispatcher is a new worker role, `agent worker --role notify`, built by a
least-privilege builder in the sole composition root modeled on the schedule
worker's, with its own systemd unit and its own environment file. That file
holds the database URL and the four APNs settings and must not hold the API
bearer token, a model-provider key, a tool or web credential, or a
browser-profile credential — the same argument the schedule role's environment
file makes, applied to a process that holds a different secret. The API,
interactive worker, async worker, maintenance worker, and schedule worker never
load the push key. Multiple dispatchers are safe.

The alternative — a sweep on the maintenance worker — was rejected because the
maintenance role reads the shared environment file every role reads, and the
push key should live only where it is used.

## The Apple client

ADR-0049's decision that the native client adds no device, notification, or
pairing concept is partly superseded, as ADR-0050 superseded its local-history
constraint: the client registers a device and renders a push, and it holds no
notification state of its own. Concretely:

- an application-delegate adaptor requests notification authorization after a
  connection is configured, calls the system registration, and on token
  receipt posts to `POST /v1/devices` with a `client_device_id` minted once and
  stored in the keychain beside the bearer token, the token, the environment
  derived from the build configuration, the platform, a device name, and the
  bundle identifier; it re-posts on launch and on token change and revokes on
  disconnect;
- the `aps-environment` entitlement is added beside the existing keychain
  entitlement, the push capability is enabled on the application identifier,
  and provisioning profiles are regenerated — owner actions outside the
  repository;
- a push payload's `veetbot` dictionary is the closed payload above; a tap
  selects the session, attaches to the run, and scrolls to the approval or
  question card through the transcript-restore path ADR-0053 established;
- a server that returns not-found for `/v1/devices` is feature-detected the
  way the client already detects a server that needs upgrading, so an older
  server keeps working;
- the CI Apple job builds for the simulator; the user-interface fixture must
  not prompt for notification permission, and the new entitlement must not
  break unsigned simulator builds.

Actionable buttons on the lock screen — approve or deny without opening the
app — are out of scope: an approval without re-authentication is a new layer in
ADR-0017's stack and needs its own decision.

## Tracked metrics

Track:

- enqueue count by kind and trigger;
- outbox depth and oldest pending age;
- dispatch attempts, outcomes, and settled status counts;
- delivery latency from enqueue to first delivered attempt, p50, p95, p99;
- token invalidations and device revocations;
- superseded, expired, and failed counts by kind;
- provider-token re-mint count and transport connection failures.

Metrics contain tenant-safe identifiers or aggregates and never payload
content, tokens, or the key.

## Build sequence

1. Add the device and notification domain values, the closed payload, and the
   deduplication-key rules with property tests. **M12.**
2. Add the three-table migration, ORM models, in-memory and PostgreSQL
   repositories, constraints, RLS, and the three port contracts. **M12.**
3. Add the in-transaction enqueue hook on the terminal writer and the
   scheduling accountant and materializer, beginning with the every-write
   crash and trigger-catalog regressions. **M12.**
4. Add the dispatcher, claim lease, staleness checks, retry schedule, and the
   fake transport; then the APNs adapter against a fake server. **M12.**
5. Add the `notify` worker role, its least-privilege builder, systemd unit,
   environment file, release validation, and wake-up. **M12.**
6. Add the seven HTTP routes, exact scopes, idempotency, cursor pagination,
   OpenAPI assertions, and the offline inbox. **M12.**
7. Add Apple client registration, token upload, revocation, entitlement, and
   deep-link handling with reducer and coordinator tests. **M12.**
8. Run the full non-live suite, the PostgreSQL integration and resilience
   lanes, the Apple lanes, hosted CI, and the required GitHub CodeRabbit loop
   on one final head. **M12.**

## Hard gates

1. **Device registration is idempotent and principal-scoped.** Re-registering
   the same `client_device_id` for the same principal updates one row in
   place; the same identifier under another principal is a distinct row; a
   cross-principal read returns not found. Registered as
   `gate.device.register_idempotent`, case. **M12.**
2. **One live token belongs to one active device.** A token re-registered from
   a new installation moves to the new row and the old row's token is nulled;
   two active devices never hold the same token. Registered as
   `gate.device.token_unique`, case. **M12.**
3. **Revocation takes effect before the next claim.** Revoking a device clears
   its token, appends `device.revoked`, and a row claimed by the next dispatch
   targets no revoked device. Registered as `gate.device.revoke_immediate`,
   case. **M12.**
4. **Device lifecycle is audited once and content-free.** Register, token
   update, revoke, invalidation, and delete each append exactly one process
   event with a stable derivation key and a token fingerprint, never the
   token. Registered as `gate.device.lifecycle_audited`, case. **M12.**
5. **The device and notification schema encodes its trust boundaries.**
   Metadata inspection proves primary and foreign keys, the partial unique
   token index, the together-or-absent token constraints, the dispatch and
   listing indexes, and row-level security on all three tables. Registered as
   `gate.device.persistence_schema`, structural. **M12.**
6. **Device and notification persistence is principal isolated.** Row-level
   security and repository predicates prevent cross-tenant and
   cross-principal reads and mutations of devices, outbox rows, and
   deliveries. Registered as `gate.device.persistence_isolated`, case.
   **M12.**
7. **Enqueue is atomic with its trigger.** Inject a crash after every write
   in the terminal writer's finalize, the scheduling accountant, and the
   materializer. No committed state holds an outbox row without its event or
   a partially written row; the only committed event without an outbox row is
   one whose enqueue failed inside its savepoint, and that failure is audited
   as a content-free process event in the same transaction and never changes
   the run's terminal state. Registered as `gate.notify.enqueue_atomic`,
   case.
   **M12.**
8. **Exactly the five transitions enqueue, and one named producer besides.**
   Approval requested, waiting for user, run failed, scheduled run accounted,
   and scheduled occurrence skipped each enqueue one row; interactive
   `run.completed`, `run.cancelled`, tool lifecycle events, message deltas,
   and surface replies enqueue nothing; the Milestone 15 health check is the
   only non-transition producer and enqueues only the two ops kinds.
   Registered as `gate.notify.trigger_catalog`, case. **M12.**
9. **Repeated triggers deduplicate.** Generated repeats — a retry after an
   unknown commit, a repeated hook invocation, two processes recording one
   transition — produce exactly one outbox row per deduplication key.
   Registered as `gate.notify.dedupe`, property. **M12.**
10. **Payloads are content-free.** A corpus of approvals, questions, failures,
    and schedule instructions carrying secrets, arguments, message text, and
    reasoning yields payloads with only the closed key set and a templated
    title; a structural walk finds no free-text field on the payload model
    beyond the title and the registry tool name. Registered as
    `gate.notify.content_free`, corpus. **M12.**
11. **Concurrent dispatch is safe and replay is bounded.** Two dispatchers
    racing on one pending row deliver it to each target once under the claim
    lease and write one delivery row per attempt; a crash between a transport
    accept and the ledger write re-sends on the next claim under the same
    collapse identifier and records a further attempt — at-least-once, never
    a lost or unrecorded send. Registered as `gate.notify.dispatch_once`,
    case. **M12.**
12. **Retry is bounded and expiry is honoured.** Transient outcomes follow the
    declared backoff schedule and stop at the ceiling as `failed`; a row past
    `expires_at` is settled `expired` and never sent. Registered as
    `gate.notify.retry_bounded`, case. **M12.**
13. **Stale notifications are suppressed.** An approval resolved or expired
    before dispatch, a question already answered, a run no longer waiting, or
    a deleted session settles `superseded` with no transport call, and session
    erasure deletes pending rows in the same transaction. Registered as
    `gate.notify.stale_suppressed`, case. **M12.**
14. **An unregistered token is invalidated once.** `Unregistered` and
    `BadDeviceToken` invalidate the device's token, append
    `device.push_token_invalidated`, and remove the device from the next
    enqueue's targets; a 5xx or 429 does neither. Registered as
    `gate.notify.token_revoked_on_410`, case. **M12.**
15. **The APNs adapter authenticates and addresses correctly.** Against a fake
    server the adapter signs ES256 provider tokens from the configured key
    file, re-mints inside the window, sends topic, push type, priority,
    collapse identifier, and expiration, selects the host from the device's
    environment, and never logs the key or a token. Registered as
    `gate.notify.apns_auth`, case. **M12.**
16. **Every notification port has executable adapter contracts.** The device
    registry, the outbox, and the push transport each have a named shared
    contract exercised by every registered adapter, including the fake
    transport. Registered as `gate.notify.port_contracts`, structural.
    **M12.**
17. **A client can read every notification while offline.** With no transport
    configured, enqueue every kind; the inbox later returns each row with its
    status and delivery outcomes through stable pagination. Registered as
    `gate.notify.offline_inbox`, case. **M12.**
18. **Notifications are default-off and credential-confined.** With both flags
    off, no route mounts, no outbox row is written, and no dispatcher builds;
    release validation requires the flags to change together; the dispatcher's
    environment file carries no bearer, provider, tool, or browser credential,
    and no other role loads the push key. Registered as
    `gate.notify.default_off`, case. **M12.**
19. **Notifications migrate cleanly from an empty database.** The migration
    chain reaches head and exactly matches the declared SQLAlchemy metadata.
    Registered as `gate.notify.migration_clean`, case. **M12.**
20. **The notification migration is reversible at its boundary.** Upgrade,
    downgrade, and re-upgrade from the immediate predecessor leave a valid
    schema. Registered as `gate.notify.migration_stepwise`, case. **M12.**

## Open questions

1. Per-device `muted_kinds` is the whole preference model. A per-principal
   preference table with a device override would be more expressive; nothing
   asks for it yet, and adding it later is additive.
2. Email and webhook transports are intentionally outside this milestone. The
   `PushTransport` port is the seam; each needs its own destination
   verification and outcome vocabulary, and the roadmap holds them as B4.
3. Interactive `run.completed` is not a notification kind. If a principal
   wants it, the kind is added to the closed set and defaults to muted.
4. `capabilities` and `granted_scopes` from Section 29.6 wait for the device
   channel. Landing them now as empty columns would be speculative.
5. Actionable lock-screen approval is a new authorization layer, not a
   notification feature, and needs its own ADR.
