# ADR-0097: Bland calling and a public receptionist

- Status: Accepted; owner approved implementation on 2026-09-11 as Milestone 27
- Date: 2026-09-11
- Related: engineering plan Sections 2.5, 21 (roadmap B11), 22, and 29;
  ADR-0017, ADR-0064, ADR-0071, ADR-0082, ADR-0090
- Proposal: [Bland calling](../bland-calling-proposal.md)
- Owner intent: make and receive telephone calls using the owner's existing
  Bland account; anyone may call the assistant; an inbound number is unconfirmed

## Context

The owner explicitly requested Bland calling. Public call reception differs from
the existing paired Telegram and WhatsApp channels: an unknown correspondent
must be able to leave a message, without becoming an authenticated owner or
receiving the owner's private context and tool authority.

The canonical plan still lists voice in roadmap B11. Its roadmap-entry rule
requires owner authorization and a specification with gates. Before this decision, no telephony
workstream existed in project state. The inbound-surface contract also
forbids content-bearing writes from unpaired senders. Implementing public calling
as an ordinary paired surface would contradict that contract. This ADR authorizes
an explicit, separate correspondence boundary under Milestone 27.

## Decisions

1. **Bland owns the live voice conversation.** Veetbot supplies an approved
   outbound brief or an owner-curated public receptionist profile and receives
   attributable call results. This does not replace Veetbot's model gateway or
   route each spoken turn through the durable run loop.
2. **Public reception has no owner authority.** Authenticated provider call
   records may be stored for the principal bound to the configured account and
   number. The caller is an external correspondent, not a paired user. Caller
   ID is not authentication. There is no call-triggered run on the caller's
   behalf, private-context upload, live Veetbot tool bridge, or automatic memory
   source admission. Existing paired channels retain their current rules.
3. **Outbound approval binds the brief and disclosure.** The owner sees the
   actual number, task, permitted facts, duration, recording choice, and agent
   revision. Every dispatch uses the existing external-message approval and
   non-idempotent effect lifecycle. Generated speech is not an exact approved
   script; binding commitments are outside this initial scope.
4. **Narrow first-party REST integration through MCP.** Read and call modes
   have distinct, fixed rosters and honest side-effect classifications. The
   remote provider's general mutation and account-administration tools are not
   exposed to the model. Provider types remain outside the core.
5. **Signed callbacks are receipts, not instructions.** Restricted ingress
   verifies the bounded signed body before parsing, then a credential-bearing
   adapter verifies call/account/number ownership. Tenant-bound transactional
   receipts deduplicate delivery and preserve deletion tombstones. A callback
   cannot assign scopes, identify its notification recipient, or authorize a
   subsequent call.
6. **Make lifecycle limitations explicit.** A possibly dispatched call is
   reconciled without redial. Cancellation after dispatch needs provider
   termination. Inbound spending begins at Bland before a callback reaches
   Veetbot; only verified provider controls can bound that admission. Inbound
   concurrency follows the reviewed provider plan's limits, independently of
   Veetbot's single concurrent outbound reservation. A stricter one-at-a-time
   inbound limit is optional, not an activation requirement.
7. **Introduce the workstream with its complete contract.** Milestone
   27 registers the canonical design and gates before implementation and does
   not advance the verified sequential ceiling. The correspondence storage and
   content-free notification trigger are explicit plan amendments. Existing
   acceptance criteria remain intact; project state records the new workstream.

## Consequences

The owner receives a public receptionist and approved outbound calling without
giving strangers a private assistant session. A private configuration ceremony,
number selection, provider limits, fixture verification, and authorized live
smoke remain necessary for activation. Local records need owner access control,
retention, erasure, reconciliation, and distinct provider-retention reporting.

Acceptance updates the canonical plan, detailed specification,
state, and registry together. This ADR is not implementation or live-release evidence. The proposal's defaults remain
reviewable; it does not imply permission to buy a number or make a live call.

## Alternatives considered

- **Pair callers to the owner:** rejected; caller ID and a public number do not
  authenticate a person or authorize use of the owner's account.
- **Give Bland a general Veetbot API token:** rejected; a voice-model prompt
  cannot enforce the deterministic policy boundary.
- **Expose the entire provider MCP catalog:** unnecessary for the requested
  runtime; administration belongs in operator setup.
- **Only configure outbound calling:** insufficient; the owner requested public
  incoming calls as well.
- **Return a realtime Veetbot response to every utterance:** deferred; it needs
  separate latency, interruption, authentication, and in-call approval contracts.
