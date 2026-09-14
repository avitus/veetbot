# ADR-0096: Ninety-day automatic email catch-up and cost recovery

- Status: Accepted — owner requested these repairs on 2026-09-13
- Date: 2026-09-13
- Supersedes: ADR-0092's unlimited historical reach, for the current rollout

## Decision

Automatic Email catch-up is limited to the latest ninety days for now. Inbox
and historical discovery use that bound; saved older pagination cannot resume
past it. Automatic assessment and learning exclude older messages, including
older passages in otherwise recent threads. Cached older correspondence remains
readable, and explicit owner-requested thread operations retain their existing
authority and complete-context requirements. Finishing the permitted window
means ninety-day retrieval is complete, not that the whole mailbox was read.

An unchanged source's completed assessment can be reused when its relevant
owner feedback, correspondent evidence and shared memories are unchanged.
Global profile churn and writing examples alone do not require another model
assessment. Cached features are rescored under the current profile. Writing
examples belong in drafting requests, not importance-assessment prompts.

Budget refusal includes spent/reserved amounts, limits and a next check time.
The native client shows one persistent pause and keeps cached browsing usable;
automatic admission waits until that time, including across mode switches.
Explicit refresh can recheck after an operator repair. Connection replacement
clears the prior account's pause. Ambiguous reservations remain charged.

## Consequences

The existing dollar ceilings, provider selection, finite slices, source
provenance and send approvals remain in force. Older historical learning needs
fresh owner authorization. This amendment changes the authorized scope of
Milestone 26 without completing a gate or authorizing production deployment.
