---
title: Bland Calling — Integration Proposal
status: approved
canonical: false
---

# Bland calling for Veetbot

**Status: approved by the owner on 2026-09-11; implementation authorized as Milestone 27.**
The canonical [calling design](plan/bland-calling.md) governs implementation.
The remaining proposal text is the retained approval artifact, not live evidence.
The owner requested Bland calling on 2026-09-11, already has an account and API
key, and confirmed that anyone should be able to call the assistant. An inbound
number has not yet been confirmed. These are established requirements; the
defaults and implementation boundaries below are proposed decisions.

[ADR-0097](adr/0097-bland-calling-and-public-receptionist.md) records the
architectural change. This proposal does not change the canonical plan, authorize
a number purchase, or claim a milestone or acceptance check is complete.

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

The initial receptionist collects the caller's stated name, organization if
relevant, reason for calling, requested callback details, and any stated deadline.
It confirms these details and says it will pass the message along. It does not
promise that a callback, booking, payment, or other action has happened.

## Live conversation and owner authority

Bland handles telephony and the live speech conversation. Veetbot owns the
approved outbound brief, public receptionist profile, call records, owner-facing
results, and any subsequent action. This first version does not stream every
spoken turn through Veetbot's durable worker.

| Boundary | Proposed rule |
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

Voice remains in roadmap B11 of the [engineering plan](plan/engineering-plan.md).
The [inbound-surfaces design](plan/inbound-surfaces.md) permits owner runs only
after pairing and stores no content from an unpaired sender. Public reception
therefore needs its own explicit correspondence-intake contract. It must not be
implemented by pairing every caller to the owner, assigning `USER` trust to
transcripts, or weakening Telegram/WhatsApp's existing pairing behavior.

The proposed architecture admits authenticated provider call records belonging
to the owner's configured number, without creating a run on the caller's behalf.
Owner-requested reads bring those records into ordinary sessions as
`EXTERNAL_UNTRUSTED` tool results. The existing communication-memory adapter
admits specific source contracts, not arbitrary MCP data; call transcripts do
not become automatic long-term memories in this first release. A later evaluated
source extension would need its own attribution, erasure, and precedence checks.

Before production code, accept the ADR, add a canonical calling specification
and its registered gates, and record the workstream in project state and the
milestone map. The proposed allocation is a new independent Milestone 27, whose
completion cannot advance the sequential verified ceiling beyond 12. The owner
has requested calling; the milestone number is a proposed organizational choice.

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

Proposed callback processing:

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
content. Initial retention is proposed at thirty days for transcripts and
summaries, with owner deletion and durable erasure of derived local copies.
Provider-side retention is a separate account setting to verify during setup;
local deletion must never be presented as deletion from Bland.

Call-result notifications would add a new trigger to the currently closed
notification catalog. The canonical design must explicitly register that
content-free trigger and owner-only deep link before automatic alerts are enabled.
The first implementation must not mislabel a public call as a completed owner run
just to obtain an existing notification.

## Configuration and activation

Keep all calling disabled by default. Proposed configuration includes separate
outbound, inbound-intake, and notification enablement; account/number binding;
private API-key and webhook-secret file paths; public-profile revision; maximum
duration; and admission/reconciliation bounds. Names become final in the
canonical specification and its configuration inventory.

Load secrets from absolute owner-only regular files outside the repository, using
secret-typed fields and the existing child-environment confinement. Credential
validation must not print values. A local bootstrap command can accept the key
through a hidden prompt; the user need not paste it into chat. Never replace an
existing account webhook secret without identifying affected integrations.

Proposed initial call limits are five minutes per call and one concurrent outbound
call. Inbound concurrency follows the reviewed provider plan's limits; a stricter
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
performed as part of this proposal.

## Acceptance evidence to register

These are proposed checks, not registered gates or passing evidence:

| Area | Required evidence |
| --- | --- |
| Default off | No call tools, listener, reconciliation, or unsolicited alerts while disabled; incomplete enabled configuration fails closed |
| Tool separation | Exact read/call rosters and scope checks; no general provider passthrough or account administration |
| Approval | The approved number, complete brief, and configuration revision are the dispatched values; denial, expiry, changes, and revocation prevent dispatch |
| Recovery | Lost responses and restart after dispatch never redial; accepted, answered, completed, and uncertain outcomes stay distinct |
| Cancellation | Before-dispatch cancellation prevents dialing; active-call stop is attempted and its provider result is represented honestly |
| Provider boundary | Fixed HTTPS origin, refused redirects, bounded bodies, schema validation, sanitized errors, and no exposed credential |
| Public-call isolation | Arbitrary callers, spoofed caller ID, and prompt injection gain no owner scope, private context, tool authority, or automatic memory authority |
| Authenticity | Valid signed callbacks work; missing/wrong signatures, wrong number/account, malformed identifiers, oversized bodies, and provider failures fail closed |
| Durability | Duplicate, reordered, delayed, missed, and replayed callbacks produce one retained result without resurrecting deleted content |
| Tenant isolation | Shared in-memory/PostgreSQL contracts and forced-RLS checks prohibit foreign call reads, mutations, and reconciliation |
| Retention | Owner deletion and expiry remove transcript/summary copies while preserving bounded content-free deduplication evidence |
| Notifications | Only the approved trigger emits a content-free owner notification; untrusted metadata cannot choose a recipient |
| Bounds | Concurrent outbound admission, provider-side inbound bounds, bounded reconciliation, and billing limitations have testable behavior |
| Live setup | Real inbound and explicitly authorized outbound smoke, signed callback delivery, voice quality, and final call result match the configured number |

Production changes begin with failing behavioral tests under Lane A, then the
focused partition and risk-relevant repository checks. The new adapter begins
with a shared contract suite. Hosted checks and CodeRabbit remain required on an
explicitly authorized PR; creating a PR, merging, and production delivery retain
their existing authorization boundaries.

## Delivery sequence

1. Accept the architecture and public correspondence boundary, then publish the
   canonical specification, ADR status, project-state authorization, and gates.
2. Implement read and approved-call tools, credential loading, durable dispatch,
   reconciliation, and cancellation with red-green evidence.
3. Implement signed inbound-result intake, private storage, owner reads, erasure,
   and the declared notification extension. Configure the reviewed Bland agent.
4. Select/provision the number and install credentials through the private setup
   ceremony. Verify account limits, perform authorized live smoke, and complete
   the repository's review and release process when requested.

Live access to private Veetbot tools, owner authentication over the telephone,
automatic bookings or payments, warm transfers, batch campaigns, SMS, a new
native Calls mode, audio storage, and automatic call-derived memory are outside
this initial proposal.
