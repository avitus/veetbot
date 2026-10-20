# ADR-0222: Owner-approved scope for the retained recall review

- Status: Accepted scope; release verification and human findings pending
- Date: 2026-10-20
- Related: ADR-0218, ADR-0220

## Decision

The owner explicitly approved 37 individual cases and the stated residual risk,
and requested updated review requirements and a shorter review packet. This
revises only the human coverage requirement in ADR-0220 for the retained
comparison. It does not supply human findings or authorize activation or a PR.

The approved `recall-owner-review-scope-proposal@1` manifest has SHA-256
`1202aab4a598d84ca9f049793fa14c83cdf088b713bd4efbcc0fe87aaed66bcb`.
It fixes all nine obsolete-final-answer cases, all 20 strict sparse-to-hybrid
regressions and eight deterministic category spot checks. All 270 omitted
previously mandatory cases remain explicitly unreviewed. One cost summary
retains all 40 over-ceiling rows across 13 scenarios; four history summaries
retain all 18 interruption, recovery and cost items.

The subset cannot certify the original quality target or represent the whole
corpus statistically. Omitted failures may contain materially different or
unacceptable behavior. Exact-output failures receive no automatic semantic
credit. All 1,600 observations, original failed gates, charges and uncertainty
remain unchanged. No new measurement or held-out tuning is authorized.

## Versioned evidence

The new `recall-owner-review-submission@2` records the exact individually reviewed
and omitted IDs, findings for every selected case and every summary, original
history item membership, and an explicit residual-risk finding and decision.
The approved scope manifest and original observation and history digests are
pinned. Caller-selected subsets or other comparisons cannot inherit approval.
Scope approval alone, blank findings, missing summary members, or relabelling a
subset as a version-one review must be refused.

Evidence and admission use `recall-bounded-evidence@2` and
`recall-bounded-admission@2`, with matching review/evidence/admission versions.
The version-one full-review route retains its existing coverage requirements.
The packet starts with empty findings and no selected decision or attestations.
It saves drafts and exports accurately scoped human submissions, including
rejection or changes-required decisions that cannot admit a rollout.
Generate it with `scripts/build_recall_owner_review.py`, providing the immutable
proposal, original review-data JSON and an unused output directory. The generator
verifies their pinned identities and never overwrites a prior packet. Admission
version two carries the exact original proposal bytes in `scope_proposal`; the
old version-one assembly helper must not convert a shorter submission.

Hashes bind content; they do not authenticate a human author. The operator must
verify the actual owner export and its provenance. No agent fills or signs the
review for the owner.

## Unchanged release requirements

The owner subsequently requested review of real production use instead of this
synthetic packet. [ADR-0223](0223-owner-production-recall-trial.md) defines that
separate trial route. This scoped packet remains retained and unreviewed; the
new direction does not fill its findings or change its version-two contract.

All access, correction, erasure, lifecycle, runtime budget, owner-local interactive
scope, seven-day maximum expiry, withdrawal and rollback requirements remain.
Scheduled or delegated use and unattended reconsolidation remain off. The new
source requires its own authorized release process, CodeRabbit GitHub review,
final-head CI, production delivery, fresh actual production compatibility and
admission evidence. Historical release clearance does not apply to this change.
Until those requirements and real owner findings are satisfied, recall remains
disabled.
