# ADR-0244: Standing owner-reviewed work-inbox junk filtering

- Status: Accepted — owner explicitly authorized permanent production on 2026-10-24
- Date: 2026-10-24
- Amends: ADR-0204 for the named owner's work account

## Decision

The owner independently reviewed every one of the seven-day pilot's 198 bypassed
conversations in batches of 102 and 96 and confirmed all were correctly classified
and none worth keeping. This establishes zero observed false positives in those
bypasses, not recall, retained-mail quality, or general-population calibration.
The owner explicitly requested permanent production filtering after the final review.

Admit a separate default-off `owner_filter` mode with standing authorization,
without a calendar expiry. Preserve the pilot's raw spam/bulk choice rule, exact
questions and pinned model, work-only owner/tenant scope, fifty new conversations
per UTC day, all pre-egress protections, existing budgets, durable cost accounting,
and Important recovery. Reuse the existing durable daily counters and conversation
admissions so switching mode or replacing evidence cannot reset the allowance.
No Gmail mutation, draft creation or memory formation is authorized by a bypass.
The general calibrated `filter` and the seven-day `pilot` are unchanged.

The private content-free packet binds a new standing policy hash to the exact
classifier policy/model, tenant, owner and literal work account. It contains the
original selected-inbox review (at least 43 unique labels, twenty correct junk
nominations and seven legitimate keeps, no false-positive nominations) and a
separate complete-pilot-bypass review of at least 198 unique owner-labeled junk
nominations, all correct. Fingerprints in each cohort must be unique; the cohorts
are separately identified because a pilot may revisit a selected-inbox source.
The packet records an aware authorization time, which cannot be in the future.
Pilot packets cannot authorize standing mode, nor standing packets the pilot.
Missing, malformed, mismatched or unreviewed evidence disables calls. Authorization
scope is rechecked before dispatch, receipt use and assessment commit. The mode,
policy and packet digest bind cached bypass assessments.

Off remains the immediate stop for new calls and admissions; ordinary assessment
restores bypassed mail through the existing bounded refresh. Changing the model
or questions invalidates standing evidence and requires fresh reviewed evidence.
The standing permission has no automatic expansion to personal mail or larger
budgets/caps. This decision does not complete Milestone 26 or its quality gates.

## Delivery and operations

Focused regressions must cover standing admission beyond seven days, evidence and
scope rejection, mode separation, ordinary keep processing, the shared daily cap,
protection and off-switch restoration. Run sidecar checks and exact-head hosted
CI/CodeRabbit review, deploy through the normal release pipeline, verify live
release identity, then install the private packet and explicitly select the mode.
Retain the expired pilot packet and private owner review artifacts for audit.
Observe content-free admission/usage records and retain reversible off rollback.
