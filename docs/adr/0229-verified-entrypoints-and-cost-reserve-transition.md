# ADR-0229: Verified entrypoints and the cost-reserve transition

- Status: Accepted as engineering decisions within the owner's repair request
- Date: 2026-10-20
- Amends: ADR-0078 and ADR-0130 cost-reserve dispatch; ADR-0164 site entry
- Scope: Existing browser and runtime reliability; no milestone or gate moves

## Context

The owner requested permanent repairs after a signed-in browser was sent through
a site's bare-origin login route and a research run failed when a model response
first crossed its cost reserve. The latter followed ADR-0130 literally: the
request was allowed to use tools, but its charge activated a fatal dispatch
guard. It never received the promised synthesis opportunity.

## Decision

For a profile with an existing private site-verification definition, acquisition
still verifies the protected page and intended account before publishing a lease.
Navigation to that same bare origin opens the verified protected path, including
retries and later requests to open the website.
Only an empty or slash path without query or fragment qualifies. Explicit paths,
and other origins retain their ordinary meaning. The
entry target comes from trusted configuration, never page text or model input.
Returned observations describe the actual destination. Authentication, account,
egress, origin and action checks remain unchanged. No session material or page
URL is added to application metadata, events or diagnostics.

When a model charge first reaches the cost reserve, refuse its unstarted tool
calls with durable `tool.budget_exhausted` results and request a tool-free final
answer from existing evidence. Refusals do not count as executed tools. The
existing budget, cancellation and deadline checks still govern that answer;
there is no extra cost or call allowance. A response to an already synthesis-only
request that nevertheless asks for tools still fails closed. Record the requested
synthesis dimension with the response so checkpoint-loss recovery can distinguish
that violation from a newly crossed boundary. Legacy responses without the field
may receive one synthesis opportunity under their unchanged hard limits.

Recovery keeps recorded results and usage, does not duplicate refusals and does
not replay effects. The existing handling of an explicitly resolved pending
approval remains unchanged. Hard cost exhaustion and repeated-tool guards remain
failures. These decisions supersede only the specified fatal cost-transition
behavior; they do not declare partial research complete.

## Verification and delivery

Regression tests must first reproduce bare-origin navigation after successful
account verification and a cost-crossing tool response. Cover explicit routes,
fresh leases, unconfigured profiles, failed verification, durable refusals,
checkpoint-loss recovery, genuine synthesis violations and hard budget limits.
Qualify each private site definition against synthetic fixtures and the live
account before activation. A configuration-only entry cannot change navigation
behavior until the corresponding code is released. Production delivery remains
subject to the existing review and release authorization boundaries.
