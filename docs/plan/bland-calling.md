---
title: Bland Calling
status: design
canonical: true
---

# Bland calling and public reception

This canonical Milestone 27 design implements the owner's approved
[first-release proposal](../bland-calling-proposal.md) under
[ADR-0097](../adr/0097-bland-calling-and-public-receptionist.md).

## Experience

Veetbot gains a dedicated phone number and two capabilities:

- **Make a call for the owner.** In Chat, the owner supplies a recipient and
  objective. Veetbot prepares a call brief, obtains the existing human approval,
  starts the call, and later reports the outcome. For example, “Call the repair
  shop and ask whether my laptop is ready” produces a proposed recipient,
  objective, and the specific identifying information permitted to leave.
- **Answer calls for the owner.** Anyone may call the dedicated number. The
  receptionist introduces itself as an AI assistant, answers using an
  owner-curated public profile, and takes a message. Veetbot receives a call
  record, transcript, and summary for the owner to read and act on.

Suggested initial greeting: “Hi, you've reached Veetbot, an AI assistant. I can
take a message for the person you are calling. Who's calling, and how can I
help?” The production greeting uses the owner's chosen public name and explains
that the conversation is transcribed and shared with them. Audio recording is
disabled by default; transcription is still part of the service.

The reviewed configuration distinguishes `assistant_name` (default `Veetbot`)
from `public_name`, the owner the assistant represents, and from `voice`, the
provider voice selection. Both inbound greetings and outbound instructions use
the assistant and owner names: “Hi, I'm Willow, Andy's assistant.” AI identity
and transcription disclosure follow that introduction. All three values are
included in the configuration revision that binds outbound approval; renaming
the assistant requires a fresh approval and an inbound configuration readback.

The initial receptionist collects the caller's stated name, organization if
relevant, reason for calling, requested callback details, and any stated deadline.
It confirms these details and says it will pass the message along. It does not
promise that a callback, booking, payment, or other action has happened.

## Live conversation and owner authority

Bland handles telephony and the live speech conversation. Veetbot owns the
approved outbound brief, public receptionist profile, call records, owner-facing
results, and any subsequent action. This first version does not stream every
spoken turn through Veetbot's durable worker.

| Boundary | Rule |
| --- | --- |
| Public caller | An external correspondent; never the owner, even if caller ID matches the owner's number |
| Inbound context | Only the separately curated public receptionist profile and the current call |
| Outbound context | Only the approved brief and its explicitly disclosed facts |
| Private context | No automatic upload of persona, memory, email, contacts, conversations, or credentials to Bland |
| Live actions | No Veetbot tool bridge, arbitrary HTTP tools, transfers, or financial/scheduling authority in the initial call agent |
| Subsequent actions | Proposed in the owner's ordinary Veetbot session and subject to the existing policy and approval flow |
| Caller claims | Attributed to the caller, with phone numbers treated as unverified contact information |

The absence of private context and backend capabilities is enforced by
configuration and transport boundaries. A prompt alone is not the control. A
caller may manipulate the conversation, but cannot gain capabilities that the
call agent never receives. Public-profile changes are owner-authenticated and
versioned; provider configuration drift disables activation until reconciled.

The owner approves an outbound **brief**, not a verbatim transcript: a voice
model generates its actual replies during the call. The approval includes the
recipient in E.164 format, caller ID, purpose, disclosed facts, maximum duration,
recording setting, and pinned call-agent configuration. A changed recipient,
brief, or configuration invalidates that approval. Veetbot must not claim to
guarantee the wording of generated speech or use this initial integration for
binding commitments.

## Repository fit and the required plan change

Voice remains in roadmap B11 of the [engineering plan](engineering-plan.md).
The [inbound-surfaces design](inbound-surfaces.md) permits owner runs only
after pairing and stores no content from an unpaired sender. Public reception
therefore needs its own explicit correspondence-intake contract. It must not be
implemented by pairing every caller to the owner, assigning `USER` trust to
transcripts, or weakening Telegram/WhatsApp's existing pairing behavior.

The architecture admits authenticated provider call records belonging
to the owner's configured number, without creating a run on the caller's behalf.
Owner-requested reads bring those records into ordinary sessions as
`EXTERNAL_UNTRUSTED` tool results. The existing communication-memory adapter
admits specific source contracts, not arbitrary MCP data; call transcripts do
not become automatic long-term memories in this first release. A later evaluated
source extension would need its own attribution, erasure, and precedence checks.

The owner approved this scope on 2026-09-11 under ADR-0097. Milestone 27
registers fourteen gates and does not advance the verified ceiling beyond 12.
Public call records are an explicit correspondence-storage exception; callers
receive no owner authority and existing paired channels retain their contracts.

## Outbound tools and lifecycle

Use a small repository-owned Bland REST client exposed through the existing MCP
adapter, following the first-party Gmail separation. Two mode-confined server
rosters keep reads and call dispatch honestly classified:

| Server | Tools | Classification |
| --- | --- | --- |
| `bland_read` | `list_calls`, `get_call` | `NETWORK_READ` / `LOW` / `READ_ONLY` |
| `bland_call` | `start_call` | `EXTERNAL_MESSAGE` / `HIGH` / `NON_IDEMPOTENT` |

The official Bland MCP server also exposes administrative mutations and broad
API passthrough. Those capabilities are unnecessary in the owner's model tool
catalog. Number purchase, agent publication, and account changes stay in operator
setup. [Bland tool reference](https://docs.bland.ai/integrations/mcp/tools)

The provider returns a call identifier on dispatch. A successful API response
means that the call was accepted, not that the recipient answered or the task
succeeded. Save the identifier, owner, approved invocation, agent revision, and
outbound correlation identifier durably. Final states distinguish no answer,
busy, voicemail, completed conversation, failure, and an uncertain dispatch.

Use the executor's existing effect watermark and uncertain-outcome handling.
A timeout, disconnection, malformed success response, or crash after possible
dispatch must never cause an automatic redial. Reconcile using provider call
history and the correlation identifier; unresolved ambiguity remains visible to
the owner. Metadata is correlation evidence, not a provider idempotency key.
Owner cancellation before dispatch prevents the call; after dispatch the
integration must attempt provider termination and report whether termination is
confirmed. Cancelling a Veetbot run alone does not end a telephone call.

The REST adapter uses the fixed Bland API origin with authenticated TLS, no
redirects, bounded response bodies, strict argument validation, redacted output,
and content-free errors. API credentials never enter tool arguments or model
context. The implementation adds no major dependency unless a separate decision
justifies it. [Send Call](https://docs.bland.ai/api-v1/post/calls),
[Call Details](https://docs.bland.ai/api-v1/get/calls-id)

## Inbound number and result delivery

First inspect the account's existing numbers without making calls or purchasing
anything. If none is suitable, select a number and plan with the owner. Bind one
verified account/number pair to the configured Veetbot principal; never derive
the owner from caller-supplied data or an arbitrary webhook metadata field.

Configure the number with the reviewed receptionist profile, explicit duration
limit, recording disabled, and a post-call callback. The callback reports a
completed call; it is not a realtime connection to Veetbot's reasoning loop.
[Inbound configuration](https://docs.bland.ai/api-v1/post/inbound-number-update),
[Post-call webhooks](https://docs.bland.ai/tutorials/post-call-webhooks)

Callback processing:

1. Terminate HTTPS at the existing proxy and route only the callback path to a
   loopback listener in a restricted telephony ingress role. The listener does
   not hold a model key or general tool authority.
2. Bound the raw body before parsing. Verify the documented SHA-256 HMAC using
   `X-Webhook-Signature` and a separate webhook secret, with constant-time
   comparison. Prove the signed-byte convention against a real provider fixture;
   do not silently accept both raw and reserialized variants. Bland's example
   reserializes JSON, so transport-level verification needs explicit evidence.
3. Treat a verified signature as provider authentication only. Validate the call
   identifier, direction, configured number, and account binding. Retrieve call
   details through the credential-bearing provider adapter before accepting an
   unknown call; invalid and foreign calls store only content-free rejection
   evidence. The ingress role enqueues a bounded call identifier for this check,
   not arbitrary caller content or a provider-chosen URL.
4. Atomically persist the normalized, principal-bound record and its receipt.
   Duplicate callbacks and retries produce one result. A replay cannot resurrect
   erased content: retain a content-free tombstone. Out-of-order updates cannot
   regress a terminal state or overwrite already accepted source content. Reject
   provider-verified calls older than the retention window, including during
   reconciliation, so expired tombstones do not reopen historical intake.
5. Make the result available to authenticated owner reads. Use bounded
   reconciliation to recover missed callbacks and incomplete outbound results;
   it reads call state and never redials. A post-call callback without a signed
   timestamp is not freshness evidence, even when its signature is valid.

[Bland webhook signing](https://docs.bland.ai/tutorials/webhook-signing)

Call records include provider identity, direction, timestamps, duration, caller
claims, transcript, summary, result status, and provenance. Full records are
owner-only. Logs, metrics, rejection records, and push payloads contain no call
content. Initial retention is thirty days for transcripts and
summaries, with owner deletion and durable erasure of derived local copies.
Provider-side retention is a separate account setting to verify during setup;
local deletion must never be presented as deletion from Bland.

The notification catalog registers `call_finished` with the fixed title “New
call result” and only call and notification identifiers. Owner-authenticated
Apple deep links open a plain-text result sheet in Chat; transcripts never enter
the push payload and no new client mode is introduced.

## Configuration and activation

All flags default to zero: `AGENT_CALL_ENABLED` enables tools, owner reads and
the result worker; `AGENT_CALL_INGRESS_ENABLED` separately enables signed intake;
`AGENT_CALL_NOTIFICATIONS_ENABLED` enables the closed notification trigger and
requires the existing notification API. `BLAND_CONFIGURATION_FILE` names the
bounded, reviewed public JSON configuration; `BLAND_API_KEY_FILE` is loaded only
by credential-bearing roles and `BLAND_WEBHOOK_SECRET_FILE` only by ingress.
The [setup guide](../bland-setup.md) defines number review, profile generation,
role grants, signature verification, activation and rollback.

`agent call-worker` polls durable receipts every ten seconds and scans one
25-ID provider page per direction per minute. Receipts use capped exponential
backoff up to one hour; due receipts are selected before the 25-record limit.
Receipt retry timestamps are offset-aware and normalized to UTC at record
creation, storage and hydration. Missing or null timestamps are immediately due.
Malformed values fail closed; existing malformed rows require operator repair
from verified receipt state with calling disabled before activation, rather than
guessing a timestamp or replaying a dispatch.
One thousand active receipts bound ingress admission. Erased-call tombstones
are content-free and retained to prevent replay. Missing provider summaries or
transcripts may be filled after completion; accepted source text and terminal
state cannot be overwritten. Local source erasure also
redacts subsequent generated content and tool arguments in affected sessions,
since later turns may paraphrase the source without retaining its identifier;
original owner messages remain intact. Active runs defer erasure. Expired
content is hidden immediately and physical artifact cleanup uses maintenance.

`agent call-ingress` uses a separate Linux user, receipt-only database role,
loopback port 8003 and a 256-KiB raw-body limit. The main owner API exposes
`call.read`, `call.cancel` and `call.delete` routes under `/v1/calls`; no public
caller obtains owner authentication or a run.

Load secrets from absolute owner-only regular files outside the repository, using
secret-typed fields and the existing child-environment confinement. Credential
validation must not print values. A local bootstrap command can accept the key
through a hidden prompt; the user need not paste it into chat. Never replace an
existing account webhook secret without identifying affected integrations.

Initial call limits are five minutes per call and one concurrent outbound call.
Inbound concurrency follows the reviewed provider plan's limits; a stricter
one-at-a-time inbound limit is optional and is not an activation requirement.
These limits do not authorize placing a live call. Outbound admission uses
durable reservations so concurrent workers cannot exceed the outbound bound.
Provider-hosted inbound calls begin before the post-call callback, so local
callback throttling cannot enforce their telephone spending or concurrency.
Activation must verify provider-side controls and the owner's chosen plan;
post-call accounting cannot be represented as a hard spending cap.

Bland documents an Agent Phone Plan with a dedicated US number and limits shared
across inbound and outbound calls. Busy behavior and supported destinations need
to be tested against the actual account. Pricing, number availability, and
account eligibility are setup decisions, not assumptions about this owner's
account. [Agent Phone Plan](https://docs.bland.ai/platform/agent-phone-plan)

Activation requires a suitable number, reviewed public name/profile, secret files,
webhook delivery configuration, and an explicit live test recipient. One inbound
call and one owner-authorized outbound call establish the external integration;
fixture tests cannot establish live voice quality, number routing, or account
permissions. No purchase, subscription change, live call, or deployment has been
performed as part of local implementation.

## Hard gates

1. **Default off.** Disabled calling exposes no tools, ingress, reconciliation or alerts; incomplete enabled configuration fails closed. **M27.**

2. **Tool separation.** Scoped read and call rosters are fixed and honestly classified with no provider administration or arbitrary API passthrough. **M27.**

3. **Approval binding.** Every outbound call requires approval of the exact recipient, brief, facts, limits and pinned configuration; changes, denial, expiry and revocation prevent dispatch. **M27.**

4. **Dispatch recovery.** Crashes or lost responses after possible dispatch never redial and preserve uncertain outcomes. **M27.**

5. **Cancellation.** Cancellation prevents undispatched calls and requests provider termination after dispatch, with an honest result. **M27.**

6. **Provider boundary.** Provider requests use fixed HTTPS origins, refuse redirects, bound responses and sanitize credentials and errors. **M27.**

7. **Public isolation.** Callers and spoofed caller IDs gain no owner scopes, private context, backend tools or automatic memory authority. **M27.**

8. **Callback authentication.** Signatures precede parsing and storage; size, direction, account, number and call ownership are validated. **M27.**

9. **Durable receipts.** Duplicate, reordered, missed and replayed callbacks produce one result without resurrecting erased content. **M27.**

10. **Repository isolation.** Shared memory and PostgreSQL contracts enforce principal isolation, forced tenant RLS and atomic admission. **M27.**

11. **Retention and erasure.** Owner deletion and thirty-day expiry erase local transcript and summary copies without claiming provider deletion. **M27.**

12. **Notifications.** The declared call-result trigger produces only content-free owner notifications and authenticated deep links. **M27.**

13. **Admission bounds.** Durable outbound reservations, bounded reconciliation and verified provider-side inbound controls enforce documented limits. **M27.**

14. **Live release.** Authorized real inbound and outbound calls, signed callbacks, voice quality and final-head review and release evidence pass before completion. **M27.**
