# WhatsApp and SMS integration — approved design

Date: 2026-08-26
Status: approved design, pre-implementation
Branch: `plan/whatsapp-sms-surfaces`

This document records the validated design for two new milestones — SMS
through the owner's iPhone and a WhatsApp business surface — plus one new
roadmap item for the WhatsApp linked-device bridge. It is a working spec in
the brainstorming sense, not a canonical `docs/plan/` document; the canonical
entry kit (ADRs, design documents, plan sections, gates) is the first
implementation deliverable and is specified in section 7.

## 1. Decisions taken with the owner (2026-08-26)

1. **SMS is the owner's iPhone, not a CPaaS number.** The agent sends texts
   as the owner and reads incoming texts through the paired iOS device. This
   is the concrete use case roadmap item B7 has been waiting for
   (engineering-plan.md:3519).
2. **SMS scope: send-as-owner and read-incoming.** No automatic replies; the
   agent never sends without the owner's physical tap.
3. **WhatsApp is phased.** Phase 1 is the official Meta WhatsApp Business
   Cloud API — the agent's own number, ToS-clean, webhook-delivered. Phase 2
   — reading the owner's personal account and sending as the owner via an
   unofficial linked-device session — is deferred behind an explicit
   risk-acceptance ADR (ToS violation, account-ban risk).
4. **Read-side purpose, both channels: all four behaviors** — triage and
   alert, remember, draft replies, act on content — implemented by routing
   messages into runs so existing machinery (M10 memory formation, M11/M19
   scheduling, M12 notifications, M4 approvals) does the work. Message
   content from third parties is untrusted input, always.
5. **Sequencing: parallel workstreams, no reordering.** M13→15 proceed as
   ordered by ADR-0061. The new milestones enter the way 16–19 did:
   independently authorized, gates green independently, the verified ceiling
   still advances only in numerical order.
6. **Structure: two milestones (Approach A).** Milestone 20 (SMS device
   channel) starts immediately — it depends only on completed Milestone 12.
   Milestone 21 (WhatsApp surface + webhook ingress) starts its documents and
   owner ceremony now; its code lands after Milestone 14 exists.

## 2. Context: what the corpus provides and what it lacks

Verified against the corpus on 2026-08-26:

- **No design exists** for WhatsApp, SMS, Twilio, or any non-Telegram
  messaging transport anywhere in `docs/` or `src/`.
- **Milestone 14 (inbound surfaces) is specified, not implemented.** Its five
  surface ports are named but carry no declared signatures; no surface module
  or `veetbot-surface.service` exists yet. The spec repeatedly states that
  new channels are additive adapters on the same port and the same pairing
  (inbound-surfaces.md:68-69, inbound-surfaces.md:702-704), and explicitly
  anticipates a webhook transport as "a second implementation of the same
  port, to be added with its secret-token check and proxy route"
  (inbound-surfaces.md:120-127).
- **Milestone 12 (notifications and devices) is complete and implemented.**
  `Device` registry, APNs push, notification outbox, least-privilege notify
  role. Device kinds today: `mobile | laptop | desktop | web | cli |
  surface`; push providers today: `apns | telegram`
  (src/agent_core/domain/devices.py:30-41).
- **A native Apple client ships** (`clients/apple/`, SwiftUI, iOS 15+ /
  macOS 12+, ADR-0049). It registers as `kind=mobile, platform=ios` with
  APNs push (DeviceRegistrationCoordinator.swift:44-51) and already handles
  approvals, questions, deep links, and Keychain-held bearer credentials.
- **The Section 29 device seams are cut but unbuilt** (roadmap B7):
  `ToolSource.DEVICE` is declared, the `device.` tool-name domain is
  reserved, device output is forced `EXTERNAL_UNTRUSTED` at registration,
  `ExecutionTarget.kind = "device"` is a policy-input field, and
  `tool.device_offline` is a declared availability outcome
  (multi-device-and-surfaces.md:71-124). Three items were left open: the
  third tool-registration source, the hand-off suspension kind, and client
  attribution (Milestone 14 closes the last).
- **No inbound third-party HTTPS exists.** Everything public today is the
  owner's own clients over bearer-token TLS through Nginx to a loopback
  API. ADR-0071 prices the gap: Gmail push was deferred because "it requires
  an inbound webhook surface, which is the infrastructure B3 and B4 price."
- **Milestone entry has a fixed shape** (precedent: 17, 18, 19 / ADRs
  0070–0072): authorizing ADR + design doc with gates + engineering-plan
  milestone section + roadmap row amendment + `project-state.yaml` block +
  milestone-map gate area and census row + readiness section + ADR index +
  mkdocs nav, landing together.

Hard platform constraints acknowledged up front:

- **iOS provides no SMS API.** Apps cannot read or silently send SMS.
  Reading rides on a Shortcuts personal automation; sending rides on the
  system compose sheet (`MFMessageComposeViewController`) where the owner's
  tap performs the send. iMessage and SMS are indistinguishable at capture.
- **WhatsApp Cloud API is webhook-only** for inbound. There is no polling
  alternative. It also enforces the 24-hour customer-service window:
  freeform business messages are allowed only within 24 hours of the user's
  last inbound message; outside it, only pre-approved template messages.
- **No official API reads a personal WhatsApp account.** Phase 2's only
  route is the multi-device linked-device protocol via an unofficial
  library (whatsmeow-class), which is a ToS violation with ban risk.

## 3. Milestone 20 — SMS through the owner's iPhone (the device channel)

The first concrete slice of Section 29's device channel. Everything here is
single-owner, default-off, and treats the phone as an authenticated device
whose *content* is untrusted.

### 3.1 The `DeviceChannel` port and the push-wake adapter

- A `DeviceChannel` protocol lands in `ports/tools.py` (where
  multi-device-and-surfaces.md:352-355 placed it): invoke a device-scoped
  tool on a specific device and return its result. v1 returns a single
  result; streaming is a later widening of the same port.
- One v1 adapter, **push-wake + poll-back**:
  1. The worker writes a pending row to a new `device_invocations` table
     (tenant RLS, idempotent by invocation id) in the same transaction that
     records the tool call.
  2. A content-free APNs push wakes the device. This is a **sixth entry in
     Milestone 12's closed trigger catalog** — an explicit, ADR-recorded
     amendment, additive and content-free like the existing five.
  3. The iOS app fetches pending invocations over the existing
     authenticated API (new device-scoped route) and posts back a result:
     `sent | cancelled | failed | expired`.
- Invocation waits are bounded (config, default ~5 minutes). An unreachable
  or silent device yields the already-declared `tool.device_offline`
  outcome; the run reports it rather than hanging. A "waiting-on-device"
  suspension kind (Section 29's open hand-off item) is named as the
  designed alternative and **not built** in M20.
- **Presence and scope revalidation**: every device-channel action
  revalidates the device's presence and granted scopes before proceeding
  (the rule multi-device-and-surfaces.md:272-279 already states), so a
  revoked device fails on its next action.

### 3.2 `device.sms.send` — the first device-scoped tool

- **Registration — the third source, designed as the seam audit
  anticipated**: the iOS app's device registration declares
  `capabilities: ["device.sms.send"]` on the existing `Device.capabilities`
  field. The tool registry exposes device tools with `ToolSource.DEVICE`
  while the declaring device is registered and unrevoked. tool-system.md's
  "exactly two sources" sentence gains a flagged amendment.
- **Execution**: invocation → push → owner taps the notification → the app
  presents a prefilled compose sheet (recipient, body) → the owner's Send
  tap performs the send → the app posts the result. The agent cannot send
  without that tap; iOS enforces it, not our code.
- **Policy**: classified `ALLOW`, with the ADR recording the argument that
  the compose-sheet tap is a non-bypassable human confirmation — stronger
  than an in-app approval — so demanding a second in-app approval would be
  duplicate ceremony, not added safety. Hardline rules still apply to the
  tool's arguments: the outbound body passes the secret-exfiltration scan,
  so a text containing a credential is blocked before it reaches the phone.
- The tool's result is forced `EXTERNAL_UNTRUSTED` per the existing
  registration rule.

### 3.3 SMS ingest — reading incoming texts

- **Capture** (iOS 17+ required for this feature): a Shortcuts personal
  automation — "When I get a message", run immediately — invokes an **App
  Intent** exposed by the Veetbot app ("Forward message to Veetbot") with
  sender and body. The intent shares the app's Keychain access group, so
  **no credential is ever stored in Shortcuts**; the intent posts
  `{channel: "sms", sender, body, received_at}` to a new device-
  authenticated ingest route.
- **Server side**: the ingest route appends the message as device-originated
  untrusted content (`actor_type = device`, origin recorded), idempotent by
  a `(sender, body, received_at)` digest, rate-capped per device. Bodies
  never appear in process logs; a dedicated secret-rule treatment covers
  exports and events the way surface replies are covered.
- **Routing**: one standing **triage session** per `(device, channel)`,
  created lazily and keyed independently of the M14 resolver (which is
  unbuilt); each inbound message continues the triage run (or seeds a new
  one when idle) with a system-framed instruction. The four behaviors ride
  existing machinery:
  - *alert*: ask the owner via `run.waiting_for_user`, which already pushes;
  - *remember*: M10 automatic memory formation over the run;
  - *act*: existing tools under existing policy and approvals;
  - *draft reply*: compose via `device.sms.send`, owner's tap approves.
- Message content is prompt-injection surface. The triage prompt frames the
  content as untrusted data; policy and approvals gate every consequential
  action; nothing in a message can widen scopes or pair anything.

### 3.4 iOS client and owner ceremony

- Client work: a settings toggle ("SMS integration") that adds the
  capability to registration; the notification category and compose-sheet
  flow; the App Intent; pending-invocation fetch and result post.
- The Shortcuts automation setup is a **documented owner ceremony** with
  verification steps (Milestone 18's email bootstrap ceremony is the
  precedent), honest about its fragility: iOS can silently disable the
  automation; ingest is best-effort glue, not an API.

### 3.5 Flags, scopes, persistence, gates

- Default-off flags per precedent (device-channel enable, SMS-feature
  enable), changing together at release validation.
- New tables (`device_invocations`, ingest receipts, triage-session
  mapping) carry tenant RLS like every M12/M14 table.
- Routes take existing `device.read` / `device.write` scopes where they fit;
  any scope-vocabulary growth is explicit in the ADR, as M12 and M14 did.
- Roughly 12 hard gates in a new `device` area, including: a foreign or
  revoked device cannot fetch or answer invocations; invocation and ingest
  idempotency; device output forced untrusted; the server never records
  `sent` without a device-posted result; `tool.device_offline` on timeout;
  presence-and-scope revalidation before every device action; RLS
  isolation; default-off; no message body in logs; outbound body passes the
  secret scan.

## 4. Milestone 21 — WhatsApp business surface and webhook ingress

The agent's own WhatsApp number on the Milestone 14 surface seam, paying the
inbound-webhook price deliberately and once.

### 4.1 Webhook ingress (the new infrastructure)

- A loopback-bound HTTP listener runs **inside the Milestone 14 surface
  role** (`agent worker --role surface`) — never in the API process —
  exposed as one Nginx location on the existing `api.veetbot.com` vhost
  proxying to it. The firewall story stays 22/80/443; the Milestone 15
  loopback-only structural gate holds; Nginx remains the sole public
  terminator.
- The listener implements Meta's subscription handshake (verify token) and
  validates `X-Hub-Signature-256` (HMAC with the app secret,
  constant-time) **before any parse**. A bad or missing signature is a
  content-free reject with a content-free receipt. Bodies are bounded.
- Three secrets — access token, app secret, verify token — load through the
  established private-file loader into dedicated secret fields with new
  secret-rule families (`whatsapp_access_token`, `whatsapp_app_secret`,
  `whatsapp_verify_token`); never the credential map, never the
  environment. Egress is confined to the fixed Meta Graph origin over
  HTTPS, no redirects, bounded bodies — the same rules the Telegram and
  Apple transports follow.

### 4.2 The adapter — additive on the M14 ports

- `PushProvider` gains a `whatsapp` member (a shared-file overlap with M14
  in `src/agent_core/domain/devices.py`, named in the ADR the way ADR-0071
  named its overlaps).
- `external_key = dm:<wa_id>`; group and thread shapes stay reserved.
- Pairing is unchanged: `/pair <code>` over WhatsApp, same default-deny,
  same scope-ceiling intersection, same revocation semantics.
- Replies reuse the M14 redaction and chunking pipeline (WhatsApp's
  4096-character limit matches Telegram's).
- **Idempotency on Meta's string message id.** This requires one flagged
  amendment to the unimplemented M14 spec: the receipt key
  `update_id BIGINT` (a `getUpdates` artifact) generalizes to
  `external_update_id TEXT`. Loss-recovery leans on Meta's multi-day
  webhook retry plus receipt dedupe; the Telegram poll-offset resume does
  not apply to webhook transports.
- **The 24-hour window is owned by the design**: outbound sends on a
  channel idle more than 24 hours use one pre-approved **utility template**
  ("Veetbot has an update — reply to view"); freeform messages flow only
  inside the window. Template registration is part of the ceremony; a gate
  observes that outside-window sends are template-only.

### 4.3 Sequencing honesty

The listener and adapter live inside M14 deliverables that do not exist yet
(the surface worker, the surface ports). M21's code lands after M14. What
starts now: the full document kit, gate registration, the Nginx location
prepared behind the default-off flag, and the **owner ceremony with real
lead time** — Meta business account, phone number, WhatsApp Business
Account, access token, and utility-template approval.

### 4.4 Gates

Roughly 12 hard gates in a new `whatsapp` area (keeping M14's `surface`
area clean), including: bad-signature content-free rejection; handshake
correctness; unpaired-sender denial inherited from the surface seam;
string-id receipt idempotency; token no-leak (corpus kind); transport
confinement to the Meta origin; template-only outside the 24-hour window;
chunked-and-redacted replies; loopback-only bind (structural); RLS;
scope-ceiling inheritance; default-off.

## 5. Phase 2 — the WhatsApp linked-device bridge (roadmap item, not a milestone)

Reading the owner's personal WhatsApp and sending as the owner requires an
unofficial linked-device session (whatsmeow-class library or a bridge such
as mautrix-whatsapp). It enters the roadmap as a new item — not designed
now — with entry conditions:

1. an explicit **risk-acceptance ADR** naming the ToS violation and the
   account-ban risk the owner accepts;
2. a **major-dependency ADR** for the sidecar (the mature libraries are Go;
   this is a new runtime component, not a Python import);
3. after Milestone 21.

Recorded in its favor for that later design: the linked-device protocol is
**egress-only** (a persistent outbound websocket, like Telegram long
polling in spirit) — no ingress infrastructure at all — and it is
owner-identity messaging, so M20's triage-session, untrusted-content, and
draft-reply patterns transfer to it more directly than M21's surface
pattern does.

## 6. Trust and security summary

- **Pairing/authentication**: M21 senders authenticate via M14 pairing;
  M20's device is authenticated by the existing bearer + device identity.
- **Content is untrusted everywhere**: an SMS from a stranger and a
  WhatsApp message alike are `EXTERNAL_UNTRUSTED` data routed into runs;
  they can never widen scopes, pair anything, or bypass policy.
- **Sends are human-gated**: SMS by the owner's physical tap (iOS-enforced);
  WhatsApp outbound by the surface reply path under existing redaction.
- **Secrets**: three new WhatsApp secret families plus SMS-body log
  hygiene; private-file loading; scanner and export coverage; no secret in
  any client or Shortcut.
- **Ingress minimalism**: one signed webhook route, loopback listener,
  least-privilege role, default-off; the API process gains no new
  unauthenticated route.
- **Security baseline is untouched**: no control in engineering-plan
  Section 22 is weakened; the new surfaces add controls (signature
  verification, template gating, compose-sheet confirmation).

## 7. Entry kit — the canonical documents this design becomes

Each milestone lands as one documentation change-set on the ADR-0070/71/72
pattern:

| Artifact | Milestone 20 | Milestone 21 |
| --- | --- | --- |
| Authorizing ADR | next free number (likely 0073) | next free number (likely 0074) |
| Design doc under `docs/plan/` | `device-channel-and-sms.md` | `whatsapp-surface.md` |
| Engineering-plan section | `### Milestone 20: SMS through the owner's iPhone` | `### Milestone 21: WhatsApp business surface` |
| Roadmap amendment | B7 row amended in place: device channel + device-scoped tools entered as M20; presence-based routing and hand-off still wait | new row for the linked-device bridge (entry: risk-acceptance ADR + dependency ADR, after M21) |
| `project-state.yaml` | `authorized` block, scope lists, number appended | same |
| Milestone map | new `device` gate area + census row | new `whatsapp` gate area + census row |
| `readiness.md` | authorized-and-specified section, fixed-form verdict | same |
| Mechanical | ADR index line, mkdocs nav lines, `evals/gates/device.yaml` | ADR index line, mkdocs nav lines, `evals/gates/whatsapp.yaml` |

Spec amendments ride along explicitly, each flagged in its ADR rather than
smuggled: the M12 trigger-catalog sixth entry (M20); tool-system.md's
registration-source sentence (M20); inbound-surfaces.md's receipt-key
generalization to `external_update_id TEXT` (M21). `make citations-fix`
runs after every edit to cited documents; `make docs-check` gates the set.

Standard ADR clauses both carry: parallel-workstream decision with the
ceiling rule ("nothing in this milestone moves the ceiling past 15"),
shared-file disclosures (M20: `devices.py`, tool-system doc; M21:
`devices.py`, `inbound-surfaces.md`, Nginx config), default-off flag
decision citing the schedule/notification routers, gate count, and
alternatives considered (including the rejected: CPaaS SMS number, folding
WhatsApp into M14, one combined milestone, webhook-in-API-process,
personal-account bridge as phase 1).

## 8. Risks

| Risk | Standing |
| --- | --- |
| M14 schedule risk cascades into M21 code | Accepted; mitigated by starting documents, gates, and the Meta ceremony now |
| Shortcuts ingest silently disabled by iOS | Accepted and documented as best-effort; ceremony includes a verification step; no server-side heartbeat pretends otherwise |
| Prompt injection via message content | Mitigated: untrusted forcing, policy + approvals on actions, owner-tap on sends |
| Message bodies are sensitive data in the event log | Mitigated: RLS, secret-rule coverage, log hygiene, existing export redaction |
| Meta template approval lead time | Mitigated: ceremony starts immediately |
| Shared-file conflicts with in-flight M13/M14 | Mitigated: overlaps named in ADRs up front |
| iOS version floor (17+) for the SMS feature | Accepted; the client supports iOS 15+ but the feature gates on 17+ |

## 9. Testing strategy

Per the repository's TDD contract:

- **Red-first registry tests**: each gate area begins with a failing
  registry-completeness test (`test_gate_registry.py` pattern), recorded as
  evidence like M19's.
- **Contract suites for every new adapter**: the `DeviceChannel` push-wake
  adapter against a fake APNs and a fake device client; the webhook
  listener against a fake Meta that signs real payloads (valid, invalid,
  replayed, oversized); both satisfying shared port contracts a fake also
  satisfies.
- **Boundary coverage on every new route**: happy path, validation,
  authorization, failure, retry — the AGENTS.md public-surface rule.
- **iOS**: XCTest for capability registration, the App Intent, and the
  compose-sheet flow (result posting on send/cancel).
- **Ceremonies**: both owner ceremonies documented with explicit
  verification steps (send a test SMS end-to-end; receive a signed Meta
  test event).
- Gates register with `check: tests/gates/pending.py::pending_gate` and
  flip to real check paths as they go green, using the recorded M19
  workaround for exercising parallel-milestone gates before the sequential
  ceiling advances.

## 10. Open verification spikes (cheap, before or during implementation)

1. Confirm on-device that the Shortcuts "When I get a message" automation
   runs immediately on the owner's iOS version and passes sender + body to
   an App Intent (five-minute manual check during the ceremony design).
2. Confirm the App Intent can read the app's Keychain access group when
   invoked from Shortcuts with the app backgrounded.
3. Confirm current Meta utility-template review turnaround and the exact
   template-category rules at ceremony time (Meta shifts these
   periodically; the design holds regardless — only ceremony wording
   moves).
