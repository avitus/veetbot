---
title: The device channel and SMS
status: design
canonical: true
---

# The device channel and SMS

## Authorization and context

The owner authorized this milestone on 2026-08-26 (ADR-0073) as a parallel
workstream on the ADR-0069 terms: its gates become green independently and
the verified sequential ceiling still advances only in numerical order.
This document designs the first concrete slice of Section 29's device
channel — roadmap item B7's device channel and device-scoped tools — with
SMS through the owner's iPhone as the concrete use case B7 was waiting
for. Presence-based routing and hand-off remain on the roadmap.

Milestone 12 delivered everything this design stands on: the device
registry with client-minted identity, the APNs transport, the notification
outbox, and the least-privilege notify role
([notifications-and-devices.md](notifications-and-devices.md)). The
Section 29 seams are already cut: `ToolSource.DEVICE` is declared, the
`device.` tool-name domain is reserved, device output is forced
`EXTERNAL_UNTRUSTED` at registration, `ExecutionTarget.kind = "device"` is
a policy-input field, and `tool.device_offline` is a declared availability
outcome ([multi-device-and-surfaces.md](multi-device-and-surfaces.md)).

iOS provides no SMS API. An app cannot read or silently send a text.
Sending rides the system compose sheet, where the owner's tap performs the
send; reading rides a Shortcuts personal automation forwarding into an App
Intent. The design is honest about both: sends are owner-confirmed by
construction, and ingest is best-effort glue the platform can silently
disable.

## The DeviceChannel port

`DeviceChannel` lands in `ports/tools.py`, the placement
[multi-device-and-surfaces.md](multi-device-and-surfaces.md) decided:
invoke a device-scoped tool on a specific device and return its result.
Version one returns a single result; streaming is a later widening of the
same port, not a second port.

One adapter implements it: push-wake with poll-back.

1. The worker writes a pending row to `device_invocations` in the same
   transaction that records the tool call, idempotent by invocation id.
2. A content-free APNs push wakes the device. This is a sixth entry in
   Milestone 12's trigger catalog, an explicit widening ADR-0073 records;
   the payload carries the invocation id and nothing else.
3. The client fetches pending invocations over the authenticated API and
   posts back exactly one result per invocation:
   `sent | cancelled | failed | expired`.

The wait is bounded by `invocation_timeout` (default 300 seconds). An
unreachable or silent device yields `tool.device_offline`, and the run
reports it rather than hanging. A waiting-on-device suspension kind — the
hand-off residue of Section 29 — is the designed alternative and is not
built here.

Every device-channel action revalidates the device's presence and its
granted scopes before it proceeds, so a revoked device fails on its next
action rather than at its next submission.

## device.sms.send

The first device-scoped tool. Registration uses the third source the seam
audit anticipated: the client's device registration declares
`capabilities: ["device.sms.send"]` on the `Device.capabilities` field
Section 29.6 defines and Milestone 12 deferred; this milestone lands it.
The registry exposes the tool with `ToolSource.DEVICE` while
the declaring device is registered and unrevoked. The
[tool-system.md](tool-system.md) sentence closing registration at two
sources gains the amendment ADR-0073 records.

Execution: the invocation names a recipient and a body; the push wakes the
phone; the owner taps the notification; the app presents a prefilled
compose sheet; the owner's Send tap performs the send; the app posts the
result. The platform enforces the tap — no code path sends without it.

Policy classifies the tool `ALLOW`. The compose-sheet tap is a
non-bypassable human confirmation, stronger than an in-app approval, so a
second approval would be duplicate ceremony rather than added safety;
ADR-0073 records the argument. Hardline rules still apply to the
arguments: the outbound body passes the secret-exfiltration scan before
any invocation row is written, so a text carrying a credential is blocked
before it reaches the phone. The tool's result is `EXTERNAL_UNTRUSTED`
like every device result.

## SMS ingest

Capture requires iOS 17: a Shortcuts personal automation — "When I get a
message", run immediately — invokes the app's App Intent with sender and
body. The intent shares the app's Keychain access group, so no credential
ever lives in Shortcuts; it posts `{channel, sender, body, received_at}`
to the device-authenticated ingest route.

The server appends the message as device-originated untrusted content
(`actor_type = device`, origin recorded), idempotent by the SHA-256 digest
of `(sender, body, received_at)`, rate-capped per device by
`ingest_daily_cap` (default 500). Bodies never appear in process logs, and
the existing export redaction covers the content events.

Routing: one standing triage session per `(device_id, channel)`, created
lazily; each ingested message continues the triage run, or seeds a new one
when the session is idle, with a system-framed instruction naming the
content as untrusted third-party data. The standing session is found
through a unique `(device_id, channel)` mapping row that pins the live
triage session id; the row is created lazily with the session and
replaced when the session rotates. The four owner-selected behaviors
ride existing machinery: alerting asks the owner through
`run.waiting_for_user`, which already pushes; remembering is Milestone
10's automatic formation over the run; acting uses existing tools under
existing policy and approvals; a draft reply goes out through
`device.sms.send`, where the owner's tap approves it. Nothing in a message
can widen scopes, pair anything, or bypass policy.

## The iOS client and the owner ceremony

The client gains: a default-off "SMS integration" setting that adds the
capability to device registration; the notification category and
compose-sheet flow; the App Intent; pending-invocation fetch and result
post. The Shortcuts automation setup is a documented owner ceremony with a
verification step (send a test message end to end), on the Milestone 18
bootstrap-ceremony precedent, and its fragility is stated: iOS can
silently disable the automation, and ingest is best-effort.

## Persistence

Two tables, both carrying the tenant RLS policy:

```text
device_invocations
  id UUID PRIMARY KEY
  tenant_id UUID NOT NULL
  device_id UUID NOT NULL REFERENCES devices(id)
  run_id UUID NOT NULL REFERENCES runs(id)
  tool_name TEXT NOT NULL
  arguments JSONB NOT NULL
  status TEXT NOT NULL           -- pending | sent | cancelled | failed | expired
  created_at TIMESTAMPTZ NOT NULL
  resolved_at TIMESTAMPTZ NULL

device_ingest_receipts
  device_id UUID NOT NULL REFERENCES devices(id)
  tenant_id UUID NOT NULL
  channel TEXT NOT NULL
  digest TEXT NOT NULL
  received_at TIMESTAMPTZ NOT NULL
  session_id UUID NULL REFERENCES sessions(id) ON DELETE SET NULL
  run_id UUID NULL REFERENCES runs(id) ON DELETE SET NULL
  PRIMARY KEY (device_id, channel, digest)
```

## Configuration and flags

Two flags, default off, changing together at release validation:
`AGENT_DEVICE_CHANNEL_ENABLED` (the port, the routes, the invocation
push) and `AGENT_DEVICE_SMS_ENABLED` (the `device.sms.send` capability
and the ingest route). With either unset, no route mounts, no
capability-derived tool registers, and no push is sent. Limits block:
`invocation_timeout` (300 s), `ingest_daily_cap` (500).

The routes take the existing `device.read` and `device.write` scopes; the
scope vocabulary does not grow.

## Exclusions

No automatic replies from the owner's number; no silent send path; no
websocket transport; no waiting-on-device suspension kind; no
presence-based routing or hand-off (roadmap B7 residue); no Android
capture; no iMessage/SMS distinction at capture. Each exclusion stays on
the roadmap or in this document's open questions.

## Build sequence

1. Port, domain values, and the two migrations, with the contract suite. **M20.**
2. The push-wake adapter against a fake APNs and a fake device client. **M20.**
3. Capability-derived registration and the policy classification. **M20.**
4. The ingest route, digest idempotency, and the triage session seeding. **M20.**
5. The iOS client work and the App Intent. **M20.**
6. The owner ceremony document and the end-to-end verification. **M20.**

## Hard gates

1. **Capability-derived registration.** A device tool is registered with
   `ToolSource.DEVICE` exactly while a registered, unrevoked device
   declares its capability; revocation or deletion removes it. **M20.**
2. **Invocation idempotency.** Replaying an invocation id creates one
   `device_invocations` row and at most one push. **M20.**
3. **Foreign device denied.** A device other than the invocation's target
   — or a revoked target — can neither fetch nor answer it. **M20.**
4. **No send without a device result.** No server code path moves an
   invocation to `sent`; only a device-posted result does. **M20.**
5. **Offline outcome.** An invocation unanswered at `invocation_timeout`
   resolves `expired` and surfaces `tool.device_offline`. **M20.**
6. **Untrusted device output.** Every device tool result carries
   `EXTERNAL_UNTRUSTED`, forced at registration. **M20.**
7. **Presence revalidated.** Every device-channel action revalidates
   presence and granted scopes; a revoked device fails on its next
   action. **M20.**
8. **Outbound secret scan.** A `device.sms.send` body matching a hardline
   secret pattern is refused before any invocation row is written. **M20.**
9. **Ingest idempotency.** A replayed `(sender, body, received_at)`
   digest stores one receipt and seeds one run. **M20.**
10. **Untrusted triage routing.** An ingested message enters the standing
    triage session as device-originated untrusted content and cannot
    resolve a consequential action to a plain allow. **M20.**
11. **No body in logs.** Message bodies appear in no process log line and
    no event besides the designated content event. **M20.**
12. **Default off.** With either flag unset, no device-channel route
    mounts, no capability-derived tool registers, and no invocation push
    is sent. **M20.**

## Open questions

1. Whether the Shortcuts "When I get a message" automation passes sender
   and body to an App Intent on the owner's iOS version — verified during
   the ceremony, before build step 5 is called done.
2. Whether the App Intent reads the shared Keychain group with the app
   backgrounded — same verification.
3. Whether a waiting-on-device suspension kind should replace the bounded
   wait once real usage shows expiry rates; that is a later ADR.
