---
title: Inbound Surfaces
status: design
canonical: true
---

# Inbound surfaces and pairing

This document specifies Milestone 14. The engineering plan states the
requirement; this document states the mechanism. It is subordinate to
[engineering-plan.md](engineering-plan.md), and it reuses rather than replaces
the Milestone 12 device registry and notification outbox, the run submission
path the HTTP API uses, the approval and input services, the policy engine,
and the least-privilege role pattern the schedule worker established.
[ADR-0064](../adr/0064-milestone-14-inbound-surfaces-and-pairing.md) records
the architectural decisions; ADR-0061 records the authorization.

Section 29.4 of the plan names Surfaces — "inbound messaging channels
(Telegram, Slack, email, and similar)" — as "a device-like client with a
presence and a capability set, unified under one session-key resolver (DM per
user, group per participant, thread shared)", and requires that "an unknown
sender on a Surface is default-denied and must complete an explicit pairing
step (one-time code, expiry, rate-limit, lockout) before any run is created on
their behalf" (engineering-plan.md:4204-4205). Section 22 repeats the
default-deny as a security-baseline item (engineering-plan.md:3827), ADR-0017
decided the pairing shape, and the seam audit found the rest: "Surfaces are
Devices with an empty capability set", the session-key resolver is "the one
genuinely new mechanism in Section 29", pairing "needs a home and an endpoint
rather than a decision", and no client is attributed on a write
(multi-device-and-surfaces.md:292-331, multi-device-and-surfaces.md:199-220).
This document is the home, the endpoint, and the resolver. The first channel is
a Telegram bot, because the owner carries a phone and a bot needs no inbound
port.

Milestone 14 follows Milestone 12 and assumes it: the `devices` table, the
notification outbox, the `PushTransport` port, and process-event lifecycle
audit exist. It adds no second device model.

## Scope

Milestone 14 delivers the second half of Section 29: a Surface through which
a paired sender reaches the agent from a messaging channel, and through which
the agent replies. It includes:

- a Surface registry on the Milestone 12 device registry — one `devices` row
  per configured bot with `kind = surface`, `platform = telegram`, and
  `push_provider = telegram`, the kind and provider Milestone 12's closed
  enums declare for this purpose — and an inbound transport port with a
  Telegram long-polling adapter;
- a least-privilege `surface` role that holds the bot token and the database
  credential and nothing else;
- the pairing ceremony: a one-time code minted by an authenticated principal,
  presented by the sender in a direct message, bound to the surface and the
  sender, with expiry, attempt limits, lockout, and revocation;
- the session-key resolver: a unique external key per surface and chat that
  maps to one session, rotated explicitly or after idle time and never reused;
- ordinary run creation for a paired message through the same submission
  function the HTTP API uses, with origin attributed on the write;
- Milestone 12 notifications delivered back to the chat through a Telegram
  outbound transport on the Milestone 12 port, and replies through a separate
  surface-reply outbox, both redacted and chunked;
- approvals and clarifying questions resolved through the existing services
  by deterministic commands or a plain reply;
- per-sender rate limits, per-tenant ceilings, pairing routes and CLI
  commands with exact scopes, and default-off flags at both the API and the
  worker.

The milestone does not include Slack or email adapters (the port is additive;
roadmap item B3); group chats or forum threads (the key shapes are reserved);
webhook delivery (designed as the alternative transport, not built);
`DeviceChannel`, device-scoped tools, presence-based routing, or the hand-off
suspension kind; media intake (a non-text update receives one notice and is
recorded as ignored); outbound file upload (artifacts are referenced by name
and identifier); streaming partial replies; Markdown rendering; or a
multi-tenant pairing user interface.

## The boundary: a paired message is an ordinary submission

A Surface originates messages and carries replies; it executes nothing and
authorizes nothing. A paired message becomes a run exactly as a message posted
to `POST /v1/sessions/{id}/messages` does — same session rules, same
idempotency, same seed event, same checkpoint, same queue — through one shared
application function rather than a second path:

```text
Telegram ---- getUpdates ----> surface role
                                   |  one transaction per update:
                                   |  receipt, pairing, authority, admission,
                                   |  session resolve, shared submit
                                   v
                        existing queue, worker, loop, policy
                                   |
                                   v
         terminal hook ---> surface-reply outbox ---> surface role ---> sendMessage
         (notifications: Milestone 12 outbox ---> surface role ---> sendMessage)
```

This yields four load-bearing invariants:

1. No session, run, message, or content-bearing record exists for a sender
   who has not completed pairing. The only writes for an unpaired update are
   the content-free receipt, which idempotency requires, and the rejection
   audit event; rejection happens before any write that carries content.
2. A paired message creates a run bound to the paired principal with scopes no
   wider than the pairing grants and the principal currently holds, through
   the ordinary submission path; the policy engine is never told that a
   surface exists, exactly as the seam audit's intersection rule requires
   (multi-device-and-surfaces.md:243-291).
3. Every inbound update is processed at most once across restarts and
   duplicate polls, because the receipt is the first write and the poll offset
   advances only from committed receipts.
4. Notifications reach the chat through the durable Milestone 12 outbox and
   replies through a separate, equally durable surface-reply outbox — a reply
   is not a notification and never enters Milestone 12's closed trigger
   catalog — both redacted and chunked; correctness never depends on Telegram
   answering.

## Delivery mode: long polling, not a webhook

The API binds to loopback behind Nginx; a webhook would add an unauthenticated
public route to the API process or a third virtual host and certificate, and
it would couple inbound delivery to API availability. Long polling needs no
inbound port, no proxy or TLS change, lives in the least-privilege worker, and
resumes after a restart from the last committed update identifier. For a
single-host personal deployment the polling latency is immaterial. A webhook
transport remains a second implementation of the same port, to be added with
its secret-token check and proxy route if a deployment ever needs it.

Telegram rejects concurrent long polls on one bot, so the poller is a
singleton per surface: it holds a tenant-scoped advisory lock for the life of
the process and calls `deleteWebhook` at startup in polling mode. Two surface
workers are therefore safe; the second waits.

## The surface role

`agent worker --role surface` is built by a least-privilege builder in the
sole composition root, in the shape of the schedule worker's: it validates the
production topology, token-mode identity, PostgreSQL storage, and an empty
provider-credential map, and it neither loads nor receives the API bearer
token or any model, tool, web, browser, or object-store credential. Its unit
and environment file are its own. The bot token is loaded from
`AGENT_SURFACE_TELEGRAM_TOKEN_FILE` through the private-file loader the browser
control plane established — an absolute, non-symlink, owner-only regular file
— into a dedicated secret field, never into the credential map and never into
the environment. Because Telegram places the token in the request path, the
adapter maps every transport failure into a closed `surface.*` vocabulary and
scrubs request targets from exceptions before anything is logged.

The role runs two loops: the inbound poll and an outbound drain of the
surface's rows — it runs Milestone 12's dispatcher with `providers =
{telegram}` against the notification outbox, and drains the surface-reply
outbox below — so the token lives in exactly one process. The `notify` role
claims only rows with an APNs target, the `surface` role only rows with a
Telegram target, and neither loads the other's secret.

## Domain model

### Surface

A Surface is a `Device` with `kind = surface` (the Section 29 kind Milestone
12 declares), `platform = telegram`, `push_provider = telegram`, the paired
chat reference as its routing token, an empty capability set, and a presence
that is the poller's last successful poll. Registering a surface inserts its
device row and appends `surface.registered`; nothing else about the device
model changes. The Telegram outbound adapter is a second implementation of
Milestone 12's `PushTransport`, living in the surface role.

### Pairing

```python
class PairingCode(BaseModel):
    id: UUID
    surface_id: UUID
    tenant_id: str
    principal_id: str
    code_hash: bytes
    code_salt: bytes
    granted_scopes: frozenset[str]
    label: str | None
    expires_at: datetime
    max_attempts: int
    attempts: int
    consumed_at: datetime | None
    created_by_principal_id: str
    created_at: datetime


class Pairing(BaseModel):
    id: UUID
    surface_id: UUID
    tenant_id: str
    principal_id: str
    sender_id: str
    sender_label: str | None
    granted_scopes: frozenset[str]
    paired_at: datetime
    revoked_at: datetime | None
    revoked_by: str | None
    last_message_at: datetime | None
```

A code is minted by an authenticated principal holding `surface.write`,
carries at least forty bits of entropy, is stored as a salted hash, is
returned exactly once, expires after ten minutes, and admits five attempts.
`granted_scopes` must be a subset of the minter's current scopes at minting.
The sender presents it as `/pair <code>`; a match consumes the code, creates
the pairing bound to `(surface, sender)`, and appends `surface.pairing.completed`.
A mismatch increments attempts, appends `surface.pairing.failed`, and after the
per-sender threshold locks the sender for one hour with
`surface.pairing.locked`; locked attempts are not verified. Revocation appends
`surface.pairing.revoked`, rotates the sender's session keys, and takes effect
before the sender's next message. A revoked pairing row remains as audit; a
sender may pair again with a new code.

### Session key

```python
class SurfaceSession(BaseModel):
    id: UUID
    surface_id: UUID
    tenant_id: str
    principal_id: str
    external_key: str
    session_id: UUID
    created_at: datetime
    rotated_at: datetime | None
```

`external_key` is `dm:<chat_id>` for a direct message. The shapes
`group:<chat_id>:<sender_id>` and `thread:<chat_id>:<thread_id>` are reserved
for the plan's "group per participant, thread shared" rule and are rejected in
Milestone 14. At most one live mapping exists per `(surface, external_key)`;
rotation stamps `rotated_at` and a rotated key is never reused. Rotation
happens on `/new`, after a configured idle period (default twenty-four hours),
when the mapped session is closed or missing, and when the mapped session's
pinned agent version is no longer current and the session is idle — which is
how a long-lived chat outlives a frozen `agent_version`, the question the seam
audit raised. A new session pins the current agent version and records the
surface in its metadata so the session index can label it.

### Receipt

```python
class InboundDisposition(StrEnum):
    SUBMITTED = "submitted"
    INPUT_DELIVERED = "input_delivered"
    COMMAND_HANDLED = "command_handled"
    REJECTED_UNPAIRED = "rejected_unpaired"
    REJECTED_LOCKED = "rejected_locked"
    REJECTED_RATE = "rejected_rate"
    REJECTED_ACTIVE_RUN = "rejected_active_run"
    REJECTED_ADMISSION = "rejected_admission"
    IGNORED_MEDIA = "ignored_media"
    IGNORED_CHAT_KIND = "ignored_chat_kind"


class InboundReceipt(BaseModel):
    surface_id: UUID
    update_id: int
    received_at: datetime
    disposition: InboundDisposition
    session_id: UUID | None
    run_id: UUID | None
    reason_code: str | None
```

The receipt is keyed by `(surface_id, update_id)` and carries no content. It
is the idempotency boundary for inbound delivery and the reverse map from a
run to the chat that asked for it.

## The ingress transaction

For each update the surface role opens one short transaction and:

1. Inserts the receipt keyed by `(surface_id, update_id)`; a replay returns
   the committed disposition and does nothing else.
2. Records `IGNORED_CHAT_KIND` for anything but a private chat and
   `IGNORED_MEDIA` for a non-text update, replying once with a bounded notice.
3. Checks the sender's lockout and per-sender rate limit; a locked or
   rate-limited sender is recorded and receives one notice per window.
4. Looks up the live pairing for `(surface, sender)`. None means
   `REJECTED_UNPAIRED`: the receipt (already written, content-free) records
   the disposition, `surface.inbound.rejected` is appended, no content is
   stored, no session or run exists, and one bounded notice per sender per
   window tells them to pair. This is Section 22's default-deny before any run
   is created; the receipt is the permitted content-free first write.
5. Handles commands deterministically, never through the model: `/pair`,
   `/new`, `/stop`, `/status`, `/approve`, `/deny`, `/help`.
6. Resolves fresh authority for the pairing's principal through the principal
   directory, records the authority version, and computes
   `scopes = pairing.granted_scopes ∩ principal.scopes`.
7. Checks admission: per-tenant active surface runs and daily and monthly cost
   ceilings, in the scheduling admission pattern; a denial is recorded.
8. Resolves the session through the session-key mapping, rotating where the
   rules above require.
9. Calls the shared submission function with the paired principal, the
   resolved session, the message text, and an origin. An active run on the
   session that is not waiting for input is `REJECTED_ACTIVE_RUN` with a
   notice ("still working; `/stop` to cancel") — reject, not queue, which is
   what the seam audit resolved for a busy session. A run waiting for user
   input receives the text as input through the existing deterministic rule.
10. Commits, then sends a best-effort queue notification.

The transaction performs no model, tool, or Telegram I/O; the adapter's
`getUpdates` happens before it and `sendMessage` after it. The poll offset
advances only from committed receipts, so a crash before commit re-delivers
the update and the receipt insert makes the retry idempotent.

Surface runs are interactive priority 0 — a human is typing — and use
interactive reserved capacity.

## The shared submission path

`PublicRunService.submit` holds the routing table the HTTP boundary uses —
new run, input to a `WAITING_FOR_USER` run, or a conflict on an active run —
together with the seed event, `run.queued`, the checkpoint seed, and
idempotency. Milestone 14 extracts that body into one application function
that takes a unit of work, a principal, a session, content, and an origin, and
both the API service and the surface ingress call it. The CLI's run service,
already a near-copy, converges on the same function. This is the Milestone 11
rule again: a surface materializes an ordinary run; it does not grow a second
submission path.

## Trust and attribution

The message of a paired sender bound to principal P is a `USER` message for
P, with `principal_id = P`, because pairing is authentication: the code was
minted by an authenticated principal and presented over a channel that
authenticates the sender to a stable identifier, which is at least as strong
as the deployment's static bearer token, and every sender who has not paired
is rejected before any content is stored. For the owner pairing their own
account this is exactly right. The seam audit's caution stands for anyone
else: a message from a third party the principal allowlisted "is not `USER`
trust in the sense the trust model means it", and the corpus had no rule for
that middle (multi-device-and-surfaces.md:324-331). Milestone 14 does not add
a label for it. Pairing a non-owner sender to the owner's principal is a
widening that requires explicit owner approval, and the lever for it is a
narrower `granted_scopes` on the pairing, not a new trust level; a dedicated
third-party label is roadmap item B3 alongside the group and thread keys it
would be needed for.

Attribution goes on the write, not on the session or run. The seed
`user.message.created` carries `actor_type = surface`, the principal, and an
`origin` of `{kind, surface_id, update_id}`; `run.queued` carries the same
plus the authority version; the receipt carries the reverse map; and the
session's metadata records the surface so clients can label it. A session is
not owned by a channel — the owner may continue the same conversation from
the Apple client — so a session column would be wrong, which is what the seam
audit concluded. The runtime-metadata context row the context builder already
renders gains `surface=telegram` for surface-seeded runs, as platform data, so
the model can keep replies short.

## Replies, notifications, approvals, and questions

A reply is not a notification. Milestone 12's trigger catalog is closed at
five run and schedule transitions, and an interactive completion is
deliberately not one of them; a surface reply is a separate outbox class,
`surface_replies`, that the run worker's terminal hook writes in the terminal
transaction only for a run whose origin is a surface, keyed by the run (one
reply per run), claimed with the same lease pattern, and drained by the
surface role. The Milestone 12 outbox, its trigger catalog, and its gates are
unchanged. The surface role sends the run's final assistant message. The text is redacted with the same secret-rule families the
export and the scanner use, split at paragraph and line boundaries into chunks
of at most 4096 characters, and sent in order with a per-chunk delivery receipt
so a retry resumes without re-sending. A failed or cancelled run sends a reason
code only, never provider text. Artifacts are referenced by name and
identifier.

Milestone 12's notifications still travel the Milestone 12 outbox and are
drained by the surface role for the surface device. The payload is
content-free — it carries a `question_id` or `approval_id` and no text — so the
surface role performs an authenticated detail read before it sends: as the
paired principal, with the pairing's intersected scopes, through the same
application services the API serves, it reads the question text from the run's
`run.waiting_for_user` event (which requires `run.read` among the granted
scopes) and the approval's summary from the approval service (which requires
`approval.read`); if the pairing lacks the scope, it sends a generic notice
("The agent has a question" / "Approval needed") with the identifier instead.
Every detail read is redacted before it leaves. With that, its
`run.waiting_for_user` notification reaches the chat as the question text; a
plain reply is routed to the waiting run as input by the existing rule. Its
`approval.requested` notification reaches the chat as "Approval needed" with
the summary and a short identifier; `/approve <id>` and `/deny <id>`
resolve through the existing approval service, idempotently, first wins, and
a sender whose intersected scopes lack `approval.resolve` is refused. Inline
keyboards are deferred: text commands give the same authority with no new
resolution entry point.

## Security

- Default-deny and pairing before any run (engineering-plan.md:3827, ADR-0017
  decision 5). An unpaired sender stores no content.
- Pairing codes: at least forty bits, salted hash, constant-time comparison,
  ten-minute expiry, five attempts, one-hour per-sender lockout, returned
  exactly once; the precedent is the browser authentication ceremony's
  single-presentation rule.
- The bot token: an owner-only private file, a secret field, never in the
  credential map, never logged; a `telegram_bot_token` family is added to the
  secret rules so the scanner, export redaction, and event checks cover it;
  every transport failure is mapped to a closed vocabulary with request
  targets scrubbed.
- Transport confinement: the fixed Telegram API origin over HTTPS, no
  redirects, the system trust store, bounded response bodies — the rules the
  Apple transport already follows.
- Tenant and principal binding: every pairing and mapping carries tenant and
  principal, tables carry the tenant row-level-security policy, and the
  connection sets the tenant the way the schedule unit of work does. A sender
  never exceeds the paired principal: `granted_scopes` is a subset of the
  minter's scopes at minting, intersected with the principal's current scopes
  at every message, and revocation is effective before the next message
  (engineering-plan.md:3271-3273).
- Outbound redaction: secrets and raw provider errors never reach the chat;
  reasoning is never in events and so never in a reply.
- Abuse controls: per-sender messages per minute, per-tenant active surface
  runs and cost ceilings, an inbound text cap, throttled unpaired notices,
  ignored media.
- The surface role holds no model, tool, sandbox, browser, or API credential
  and needs outbound HTTPS to Telegram only; this is the one role besides the
  browser control plane that needs any egress, and the deployment page says so.

## Persistence

Milestone 14 adds six tables, all carrying the tenant row-level-security
policy (the lockout table through its surface):

```text
surface_pairing_codes
  id UUID PRIMARY KEY
  surface_id UUID NOT NULL REFERENCES devices(id)
  tenant_id TEXT NOT NULL
  principal_id TEXT NOT NULL
  code_hash BYTEA NOT NULL
  code_salt BYTEA NOT NULL
  granted_scopes JSONB NOT NULL
  label TEXT NULL
  expires_at TIMESTAMPTZ NOT NULL
  max_attempts INTEGER NOT NULL
  attempts INTEGER NOT NULL DEFAULT 0
  consumed_at TIMESTAMPTZ NULL
  created_by_principal_id TEXT NOT NULL
  created_at TIMESTAMPTZ NOT NULL

surface_pairings
  id UUID PRIMARY KEY
  surface_id UUID NOT NULL REFERENCES devices(id)
  tenant_id TEXT NOT NULL
  principal_id TEXT NOT NULL
  sender_id TEXT NOT NULL
  sender_label TEXT NULL
  granted_scopes JSONB NOT NULL
  paired_at TIMESTAMPTZ NOT NULL
  revoked_at TIMESTAMPTZ NULL
  revoked_by TEXT NULL
  last_message_at TIMESTAMPTZ NULL

surface_sender_lockouts
  surface_id UUID NOT NULL REFERENCES devices(id)
  sender_id TEXT NOT NULL
  failed_attempts INTEGER NOT NULL
  window_started_at TIMESTAMPTZ NOT NULL
  locked_until TIMESTAMPTZ NULL
  PRIMARY KEY (surface_id, sender_id)

surface_sessions
  id UUID PRIMARY KEY
  surface_id UUID NOT NULL REFERENCES devices(id)
  tenant_id TEXT NOT NULL
  principal_id TEXT NOT NULL
  external_key TEXT NOT NULL
  session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE
  created_at TIMESTAMPTZ NOT NULL
  rotated_at TIMESTAMPTZ NULL

surface_inbound_receipts
  surface_id UUID NOT NULL REFERENCES devices(id)
  update_id BIGINT NOT NULL
  received_at TIMESTAMPTZ NOT NULL
  disposition TEXT NOT NULL
  session_id UUID NULL REFERENCES sessions(id) ON DELETE SET NULL
  run_id UUID NULL REFERENCES runs(id) ON DELETE SET NULL
  reason_code TEXT NULL
  PRIMARY KEY (surface_id, update_id)

surface_replies
  id UUID PRIMARY KEY
  surface_id UUID NOT NULL REFERENCES devices(id)
  tenant_id TEXT NOT NULL
  principal_id TEXT NOT NULL
  run_id UUID NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE
  chat_ref TEXT NOT NULL
  status TEXT NOT NULL
  chunks_total INTEGER NULL
  chunks_sent INTEGER NOT NULL DEFAULT 0
  attempts INTEGER NOT NULL DEFAULT 0
  next_attempt_at TIMESTAMPTZ NOT NULL
  claimed_by TEXT NULL
  claimed_until TIMESTAMPTZ NULL
  created_at TIMESTAMPTZ NOT NULL
  settled_at TIMESTAMPTZ NULL
```

Partial unique indexes keep one live pairing per `(surface_id, sender_id)
WHERE revoked_at IS NULL` and one live mapping per `(surface_id,
external_key) WHERE rotated_at IS NULL`; an index on `run_id` serves the reply
path. Per-chunk progress lives on the `surface_replies` row (`chunks_sent`), so a
retry resumes at the next chunk. The migration follows the Milestone 13 head. Erasure: deleting
a session cascades its mapping and nulls its receipt links; revoking a pairing
keeps the row, rotates the sender's mappings, and leaves the sessions, which
are the principal's; a pairing is hard-deleted only after revocation.
Receipts carry no content and survive session erasure because idempotency must.

## Public API, CLI, scopes, and flags

Routes, mounted only when `AGENT_SURFACE_API_ENABLED` is set:

```text
GET    /v1/surfaces                                  surface.read
GET    /v1/surfaces/{surface_id}                     surface.read
POST   /v1/surfaces/{surface_id}/pairing-codes       surface.write
GET    /v1/surfaces/{surface_id}/pairings            surface.read
POST   /v1/surfaces/pairings/{pairing_id}/revoke     surface.write
DELETE /v1/surfaces/pairings/{pairing_id}            surface.write
```

`POST .../pairing-codes` requires `Idempotency-Key` and returns the code
exactly once. Two exact scopes, `surface.read` and `surface.write`, extend the
closed vocabulary in the form [http-api-and-streaming.md](http-api-and-streaming.md#the-scope-vocabulary-is-closed-and-matched-exactly)
states. The CLI gains `agent surface list | pair | pairings | revoke`. Two
flags, `AGENT_SURFACE_API_ENABLED` and `AGENT_SURFACE_WORKER_ENABLED`, default
off; production release validation requires them to change together, and the
surface unit joins the release's unit list. A `surfaces:` block in the
versioned limits file declares poll timeout, session idle period, code expiry
and attempts, lockout, per-sender rate, tenant ceilings, chunk size, and the
inbound text cap.

## Events and audit

Process events, because a surface and a pairing exist outside any session:

```text
surface.registered
surface.pairing.code_issued
surface.pairing.completed
surface.pairing.failed
surface.pairing.locked
surface.pairing.revoked
surface.inbound.rejected
surface.poll.resumed
```

They carry identifiers, dispositions, reason codes, and the authority version;
never a code, a token, message text, or a sender's content. The session and
run event logs carry the origin on the seed message and the queued run as
described above and are otherwise unchanged.

## Configuration and deployment

Surfaces are default-off. Enabling them requires PostgreSQL storage, token-mode
identity, a durable principal directory, both flags, a private bot-token file,
and the `surfaces:` limits block. The surface role runs as
`deploy/systemd/veetbot-surface.service` with its own environment file in the
shape of the schedule role's; `release.sh` validates the flag pair and the
token file and restarts the unit with the others. The `notify` and `surface`
roles are distinct processes: each holds one secret.

## Tracked metrics

Track:

- updates received by disposition; unpaired and locked rejections;
- pairing codes issued, completed, failed, expired; lockouts;
- session-key rotations by reason;
- inbound-to-queued latency p50, p95, p99; poll lag;
- replies sent, chunks per reply, redactions applied, delivery failures;
- approvals and inputs resolved from the chat;
- per-sender and per-tenant limit hits.

Metrics carry tenant-safe identifiers or aggregates, never content or tokens.

## Build sequence

1. Add the pairing, session-key, and receipt domain values, the external-key
   grammar, and the rotation rules with property tests. **M14.**
2. Add the five-table migration, ORM models, in-memory and PostgreSQL
   repositories, constraints, RLS, erasure, and the port contracts. **M14.**
3. Extract the shared submission function from the public run service and
   converge the CLI on it, keeping every existing API and CLI test green.
   **M14.**
4. Add the pairing ceremony, the ingress transaction, and the deterministic
   commands, beginning with the unpaired-denied, idempotency, and every-write
   crash regressions. **M14.**
5. Add the Telegram transport adapter against a fake server, the surface role,
   its builder, unit, environment file, release validation, and the singleton
   poll lock. **M14.**
6. Add the reply path on the Milestone 12 outbox, chunking, redaction, and the
   approval and question round-trips. **M14.**
7. Add the six routes, exact scopes, the CLI commands, rate limits, admission,
   and OpenAPI assertions. **M14.**
8. Run the full non-live suite, the PostgreSQL integration and resilience
   lanes, hosted CI, and the required GitHub CodeRabbit loop on one final
   head. **M14.**

## Hard gates

1. **An unpaired sender creates nothing content-bearing.** A message from an
   unknown sender creates no session, run, message, or stored content; its
   only writes are the content-free receipt and the rejection audit event; it
   receives one throttled notice. Registered as
   `gate.surface.unpaired_denied`, case.
   **M14.**
2. **The pairing ceremony is single-use and bound.** A code is hashed, expires,
   admits a bounded number of attempts, compares in constant time, is returned
   exactly once, and binds the sender to the minting principal and the granted
   scopes. Registered as `gate.surface.pairing_ceremony`, case. **M14.**
3. **Failed pairing locks the sender.** Reaching the attempt threshold locks
   the sender for the configured period; locked attempts are not verified and
   the lockout is audited. Registered as `gate.surface.pairing_lockout`, case.
   **M14.**
4. **A paired message is an ordinary run.** A paired text yields one session,
   one run, one seed event, and one checkpoint through the shared submission
   function, at interactive priority, as a `USER` message for the paired
   principal. Registered as `gate.surface.paired_submits_ordinary_run`, case.
   **M14.**
5. **Inbound delivery is idempotent.** A replayed update identifier after a
   restart or a duplicate poll yields one receipt and one run; the poll offset
   resumes from committed receipts. Registered as
   `gate.surface.inbound_idempotent`, case. **M14.**
6. **Ingress is atomic across a crash.** Inject a crash after every write in
   the ingress transaction; no partial receipt, mapping, session, or run
   survives and the update is re-delivered once. Registered as
   `gate.surface.ingest_atomic`, case. **M14.**
7. **The session key is stable, rotatable, and never reused.** Messages map to
   one session; `/new`, idle time, a closed session, and a stale agent version
   rotate; a rotated key is never reused; a new session pins the current agent
   version. Registered as `gate.surface.session_key_stable`, case. **M14.**
8. **Input routing follows the run's state.** A run waiting for user input
   receives the text as input on the same run; a running run yields a notice
   and no second run. Registered as `gate.surface.input_routing`, case.
   **M14.**
9. **Revocation takes effect before the next message.** A revoked pairing is
   denied before any write on the sender's next message, and its session keys
   are rotated. Registered as `gate.surface.revocation_immediate`, case.
   **M14.**
10. **Scopes are a ceiling, intersected fresh.** The run stamps the pairing's
    granted scopes intersected with the principal's current scopes; a scope
    the principal loses disappears at the next message; the authority version
    is recorded. Registered as `gate.surface.scope_ceiling`, case. **M14.**
11. **The bot token never leaks.** Over a corpus of transport failures,
    events, logs, spans, receipts, exceptions, and exports, the token is
    absent; the loader rejects unsafe files; the secret-rule family matches
    it. Registered as `gate.surface.no_token_leak`, corpus. **M14.**
12. **Replies are chunked, ordered, and redacted.** A long reply arrives as
    ordered chunks of at most 4096 characters; a retry resumes without
    duplicates; secret-shaped text is redacted; a failure carries a reason
    code only. Registered as `gate.surface.reply_chunked_redacted`, case.
    **M14.**
13. **Approvals and questions round-trip.** An approval notice reaches the
    chat and `/approve` resolves it idempotently, first wins; a sender without
    `approval.resolve` is refused; a plain reply answers a waiting question.
    Registered as `gate.surface.approval_roundtrip`, case. **M14.**
14. **Rate and admission limits hold before any write.** Per-sender rate and
    per-tenant active-run and cost ceilings are enforced before any content
    is stored. Registered as `gate.surface.rate_limited`, case. **M14.**
15. **Surfaces are default-off.** With either flag off the routes are absent
    and the worker refuses to start; the worker's environment carries no
    provider, bearer, or browser credential. Registered as
    `gate.surface.default_off`, case. **M14.**
16. **The transport is confined.** The adapter reaches only the configured
    Telegram origin over HTTPS, follows no redirects, bounds response bodies,
    and maps failures to a closed vocabulary. Registered as
    `gate.surface.transport_confined`, structural. **M14.**
17. **The surface schema encodes its trust boundaries.** Metadata inspection
    proves keys, the partial unique pairing and mapping indexes, cascade and
    erasure rules, and row-level security. Registered as
    `gate.surface.persistence_schema`, structural. **M14.**
18. **Surface persistence is principal isolated.** Row-level security and
    repository predicates prevent cross-tenant and cross-principal reads and
    mutations. Registered as `gate.surface.persistence_isolated`, case.
    **M14.**
19. **Every surface port has executable adapter contracts.** The transport,
    pairing, session-key, receipt, and admission ports each have a named
    shared contract exercised by every registered adapter. Registered as
    `gate.surface.repository_contract`, structural. **M14.**
20. **Surfaces migrate cleanly from an empty database.** The migration chain
    reaches head and exactly matches the declared SQLAlchemy metadata.
    Registered as `gate.surface.migration_clean`, case. **M14.**
21. **The surface migration is reversible at its boundary.** Upgrade,
    downgrade, and re-upgrade from the immediate predecessor leave a valid
    schema. Registered as `gate.surface.migration_stepwise`, case. **M14.**

## Open questions

1. A third-party sender's trust label. Milestone 14 has no non-principal
   sender; pairing anyone else to the owner's principal is an owner decision
   and the lever is `granted_scopes`. A dedicated label is roadmap item B3.
2. Group and thread keys are reserved, not built; their session-sharing rules
   need a use case.
3. Inline-keyboard approvals are deferred; text commands carry the same
   authority with no new entry point.
4. Whether `runs` should gain origin columns. Attribution on the write is
   sufficient for the first client; a column is additive.
5. Slack and email transports are additive on the same port and the same
   pairing; whether they are a milestone or backlog is the owner's call.
