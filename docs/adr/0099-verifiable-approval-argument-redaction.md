# ADR-0099: Approval argument redaction stays verifiable

- Status: Accepted — owner requested this repair on 2026-09-15
- Date: 2026-09-15
- Amends: the approval read contract in
  [policy-and-approvals.md](../plan/policy-and-approvals.md) and the Review & Send
  verification in [apple-client.md](../apple-client.md)

## Context

An approval stores a redacted view of its action arguments. A value under a
sensitive key or matching a credential shape becomes `[REDACTED]`, and a string
over 512 characters is truncated to that prefix plus a marker. Separately, a
client presenting an approve-once action must verify that the frozen action is
exactly the content it displays; for Email that means the tool, account,
provider thread, recipients, subject and body of the frozen send.

These two requirements were in direct conflict and the conflict was not
theoretical. Exact equality against a truncated view can never succeed, so every
reply longer than 512 characters failed verification: the review sheet never
opened, the final send control was unreachable, and the send stayed parked as a
pending approval until it expired. Shorter replies passed, which is why the
Gmail smoke tests did not expose it. Redaction that destroys verifiability
silently converts a safety mechanism into an outage.

## Decision

Redaction publishes enough to verify what it withholds. An approval carries
`argument_digests`, mapping an argument name to the SHA-256 of that argument's
full value, for every argument the view truncated **for length**. A client
verifies such an argument by digesting its own copy, which establishes the whole
value rather than the retained prefix.

A value redacted for sensitivity is never digested. The owner has no reason to
verify a credential, and a digest of a short secret is a brute-force target, so
withholding remains absolute where withholding is the point.

A truncated argument with no digest is unverifiable. A client refuses it rather
than presenting it as confirmed; absence of evidence is not equivalence.

Three alternatives were rejected. Serving unredacted arguments to the owner
reverses a deliberate redaction on a route that also carries approvals whose
arguments contain credential-shaped values. Having the client recompute
`normalized_arguments_hash` duplicates canonical-argument normalization in a
second language, where drift is silent and surfaces as a refusal to send.
Verifying only the retained prefix weakens the documented exactness guarantee
precisely where a substitution would hide.

Neither `arguments` nor `argument_digests` is the integrity boundary.
`normalized_arguments_hash` continues to cover the full normalized arguments of
the tool invocation, and dispatch reads the invocation rather than this view, so
a truncated view could never have produced a truncated action.

## Consequences

Approval reads gain one field. It is absent on records written before this
decision, so those approvals read as unverifiable and a client refuses them:
existing parked sends must be re-proposed rather than approved from a composer.
The digests describe top-level arguments only; truncation nested inside an
object or array stays unverifiable, and a client must keep refusing it. A digest
discloses nothing to a party that does not already hold the value, and confirms
only equality to one that does.

This amends the approval read contract and the client's verification mechanism
without completing a gate or authorizing production deployment.
