# ADR-0213: Owner-requested calls without confirmation, and scheduled calls that can call

- Status: Accepted under the owner's implementation instruction
- Date: 2026-10-17
- Amends: ADR-0097, ADR-0150, ADR-0172
- Scope: Existing calling, scheduling and standing-authorization surfaces; no gate moves

## Context

On 2026-10-17 the owner asked Veetbot to call Riv at 6:15 p.m. with a reminder.
Veetbot asked the owner to approve a `schedule.create` card, which the owner did.
At 6:15 the occurrence answered that no calling integration was available, and
made no call. Two causes combined:

- `schedule.create` always pinned `requested_scopes` to the empty set, except
  for the website binding of ADR-0172. The occurrence's run therefore lacked
  `mcp.bland_call.use`, and its roster had no call tool. The scheduler role also
  derived no calling scopes, so it could not have vouched for one at firing.
- ADR-0097's approval binding required a confirmation card for every outbound
  call, and the policy engine refuses to let a profile relax it. A scheduled
  occurrence has nobody watching for that card.

The owner instructed that a requested call must not fail this way again, and
that a call the owner asks for must not need confirming each time.

## Decision

**Call schedules carry calling authority.** `schedule.create` 1.3.0 accepts
`place_call=true`. It requests exactly `mcp.bland_call.use` and
`mcp.bland_read.use`, so the occurrence can place the call and read its
outcome. The model selects the capability by flag and never names a scope.
With calling not composed, creation fails with `schedule.calling_unavailable`
and leaves no schedule, instead of failing at firing. Version 1.2.0 stays
registered for sessions already pinned to it. The scheduler role derives the
two calling scopes from `AGENT_CALL_ENABLED` exactly as it derives Gmail scopes,
reading only the non-secret `BLAND_CONFIGURATION_FILE` and never the Bland key.

**The owner's request is the call's authorization.** An application standing
authorizer, composed when calling and `AGENT_CALL_OWNER_REQUESTS_ENABLED`
(default `1`) are both on, satisfies the approval the deterministic policy
requires for `mcp.bland_call.start_call`, and for a `schedule.create` with
`place_call=true` and no website binding. It allows only when all hold:

- the run is a top-level interactive run of the current agent version, and its
  principal holds `mcp.bland_call.use`;
- the run's seed is the owner's own message, or a scheduler seed in a schedule
  session;
- the newest user message has `USER` trust;
- the turn's origin trust is platform, trusted configuration, user, or memory;
- no item anywhere in the conversation, in any turn, is email, web, knowledge
  or call-record content, or a message ingested from a device; and
- every number dialed appears in the owner's own messages in the conversation.
  A scheduled occurrence's seed is the owner-approved instruction, so its
  number counts. Formatting differs freely, but the complete number must
  match: a ten-digit national number equals its `+1` form, because the
  deployment's calling number is North American, and no other prefix or
  shared suffix matches.

Origin trust resets at each user message, so an email read in an earlier turn
would not taint a later "go ahead"; the conversation-wide check closes that.
Memory can hold email-derived statements, so a number found only in memory or
People keeps the card: content can never choose who is called. Memory remains
admissible origin for the rest of the turn, so the owner's own context can
shape the brief. Every other call keeps the ordinary confirmation card.
Hardline rules and denials still precede the authorizer. Like ADR-0209, this is an application
authorization receipt after deterministic evaluation; the compiled policy, its
version, and the memory release evidence bound to it are unchanged. The policy
engine's refusal to let a profile turn calls into allows is unchanged too.

Every other part of ADR-0097's binding stays: dispatch still checks the pinned
configuration revision, caller ID, recording setting and duration limit; the
durable reservation, uncertain-outcome handling and one-active-call admission
still apply. Setting `AGENT_CALL_OWNER_REQUESTS_ENABLED=0` restores a
confirmation card for every call.

## Consequences

Hard gate 3 of the calling design now reads: every outbound call is either
approved by the owner on its card or authorized by the owner's own request, and
a content-derived call always needs the card. The calling runtime gate keeps
its approval case with the switch off; a new gate covers the owner request and
the scheduled call from request to dispatch.

Production needs `AGENT_CALL_ENABLED=1` and the existing
`BLAND_CONFIGURATION_FILE` mirrored into the scheduler role's environment.
Without it, a call schedule's occurrence fails visibly as an authorization
failure rather than silently lacking a call tool.

## Alternatives considered

- **A policy-profile tool rule allowing calls.** The engine forbids it on
  purpose, and it would move the policy version and void the bound memory
  release evidence.
- **Granting calling scopes to every schedule.** Wider than needed; a briefing
  that never calls would carry calling authority.
- **Inferring a call schedule from the instruction text.** "Remind me to call
  Mom" names a call the owner makes; text cannot decide which party dials.
