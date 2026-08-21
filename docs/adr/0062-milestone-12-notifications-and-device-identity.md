# ADR-0062: Milestone 12 notifications and device identity

- Status: Proposed
- Date: 2026-08-20
- Related: Sections 16, 21, 22, and 29 of the engineering plan; ADR-0006,
  ADR-0010, ADR-0011, ADR-0017, ADR-0034, ADR-0049, ADR-0050, ADR-0051,
  ADR-0053, ADR-0059, ADR-0061
- Detailed design: `docs/plan/notifications-and-devices.md`

## Context

The platform can park a run on an approval, suspend it on a question, fail
it, and finish a scheduled occurrence with nobody watching, and it has no way
to tell the person who owns the run. Section 29.8 deferred the `Device` concept
and notifications "to a milestone with concrete use cases"; Milestone 11's
design explicitly left push delivery out because the `NotificationService`
seam "has no delivery contract". The seam audit (ADR-0034) found that name to
be "a port name with nothing behind it" and asked whether it is one port or
two.

The owner authorized Milestone 12 on 2026-08-20 (ADR-0061) as the first
roadmap milestone, with Apple Push Notification service to the existing native
client as the first transport. The native client (ADR-0049) is transport-only
and was specified to add no device or notification concept; that constraint
was already partly superseded once by ADR-0050 for local history.

## Proposed decisions

1. **Two ports, not one, and the broadcaster stays.** `NotificationOutbox`
   (durable rows: enqueue, claim, record, settle, list) and `PushTransport`
   (deliver one payload to one device token and return a closed outcome) replace
   the name `NotificationService`, which is retired. The existing
   `LiveEventBroadcaster` remains the answer for an open connection; it is not
   given durability and the outbox is not given fan-out.
2. **The outbox row is written in the triggering transaction.** The single
   terminal writer and the scheduling accountant and materializer enqueue inside
   their own units of work, through an injected in-transaction callable wrapped
   in a savepoint, so an enqueue failure never changes a run's terminal state
   (the rule ADR-0051 set for the memory hook). No interactive request path
   talks to a transport.
3. **Exactly five triggers.** Approval requested, waiting for user input, run
   failed, scheduled run accounted, and scheduled occurrence missed or skipped.
   Interactive `run.completed` is not in the closed set.
4. **Payloads are content-free.** A closed model: kind, identifiers, a closed
   status, the registry tool name, and a templated title. No message, argument,
   approval summary, question, schedule instruction, reasoning, or traceback
   ever enters a payload; the client fetches content after the tap,
   authenticated.
5. **Delivery is at-least-once from durable rows and idempotent by key.** A
   unique deduplication key in the derivation-key idiom; a claim lease with
   `FOR UPDATE SKIP LOCKED`; a per-attempt delivery ledger; a closed retry
   schedule; staleness checks that settle a superseded row without a transport
   call; and the transport's collapse identifier as the last line of defence.
6. **A dedicated least-privilege role holds the push key.** `agent worker
   --role notify`, built by the sole composition root, with its own systemd
   unit and environment file carrying the database URL and the APNs settings
   and nothing else. No other role loads the key. The alternative — a sweep on
   the maintenance worker — was rejected on credential placement.
7. **APNs over HTTP/2 with a file-mounted provider key.** ES256 provider tokens
   from a `0600` key file, re-minted inside Apple's window; the host is chosen
   per device from a recorded push environment; `Unregistered` and
   `BadDeviceToken` invalidate the token once and audit it. The HTTP client's
   `h2` extra is the one new dependency this milestone introduces.
8. **The `Device` table lands now; the channel does not.** Registry, refresh,
   revoke, delete, a client-minted installation identity unique per principal,
   a partial unique index on live tokens, and per-device muted kinds.
   `capabilities` and `granted_scopes` from Section 29.6 wait for a consumer.
9. **Device lifecycle is audited as process events.** The precedent Milestone
   11 set for schedules resolves the seam audit's second open question without
   a new audit table, because a device has no session.
10. **Three exact scopes and seven routes, default-off.** `device.read`,
    `device.write`, `notification.read`; device registration, listing, point
    read, revoke, delete, a test-notification route, and an offline
    notification inbox; two feature flags that production release validation
    requires to change together; a provider selector in the form the web and
    browser providers use.
11. **The Apple client registers and renders; it holds no notification state.**
    ADR-0049 decision 2 is superseded to that extent and no further. Actionable
    lock-screen approval is explicitly out of scope because it is an
    authorization layer, not a notification feature.
12. **Two gate areas.** `device` (six gates) and `notify` (fourteen gates), both
    declared by one specification, because device identity is the half of this
    milestone that Milestone 14 reuses and deserves its own census line.

## Consequences

- Scheduled runs, approvals, and questions become actionable from a phone
  without opening the app first; the approval, question, and result remain
  readable through the existing API whether or not the push arrived.
- Three tables, three ports, three scopes, seven routes, one worker role, one
  systemd unit, one environment file, and one dependency extra are added.
- Session erasure gains one obligation: delete the session's pending outbox
  rows in the same transaction.
- The native client gains the `aps-environment` entitlement and a
  registration coordinator; the owner must create an APNs key and enable the
  push capability outside the repository.
- The verified gate ceiling is unaffected until Milestones 10 and 11 close;
  Milestone 12's twenty gates may become green earlier.

## Alternatives considered

- **Give `NotificationService` a body as one façade:** rejected; it hides the
  durability split the seam audit warned about.
- **Publish pushes from the live-event broadcaster:** rejected; it is
  best-effort and in-process, and a lost push would be a lost notification.
- **Poll from the Apple client instead of pushing:** rejected; background
  fetch is unreliable on iOS and the approval use case needs prompt delivery.
- **Email as the first transport:** rejected for Milestone 12; it needs address
  verification and a different outcome vocabulary, and the Apple client
  already exists. It remains roadmap item B4 behind the same port.
- **Actionable approve-or-deny buttons:** deferred; an approval without
  re-authentication is a new layer in ADR-0017's stack.
- **A `device_events` table:** rejected in favour of process events; revisit
  only if a principal-facing device-history route is wanted.
