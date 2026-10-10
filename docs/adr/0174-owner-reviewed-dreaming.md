# ADR-0174: Daily dreaming proposals with owner-approved application

- Status: Accepted — owner authorized implementation on 2026-10-10
- Date: 2026-10-10
- Related: ADR-0169, ADR-0170, ADR-0171
- Governing design: `docs/plan/memory-reconsolidation.md`

## Decision and authorization

The owner selected automatic daily previews, requested that their explicit review
apply changes to production memory, and instructed implementation of the private
web dashboard, persistent proposals and individual **Approve and apply** workflow.
This authorizes a supervised production path while unattended application remains
disabled. It does not mark failed evaluations as passed or reopen completed reviews.

Add an independently configured reviewed-dreaming mode. A daily bounded maintenance
slice uses the existing eligible-source inventory, proposal/verifier, local planners,
spend reservations and erasure rules. It saves locally validated operations as
pending owner review. Pending operations never suppress originals or enter recall,
including historical recall, derived browsing or existing-session deltas. The
existing operation/dependency store owns pending content, so corrections, erasure,
source expiry and deletion invalidate it through the existing cleanup path.

The owner-only web page lists proposed and completed operations with complete
currently visible supporting memories. It provides source inspection, explicit
approval, rejection, safe retry, merge undo, persistent run history and pause/resume.
No model or source content is trusted as HTML. Private content and credentials are
not stored in browser storage. API access requires the existing authenticated owner
and memory scopes; the public page shell contains no private data. Responses are
private/no-store and restrictive content policy prevents third-party connections.

Approval is a revision-checked, idempotent transaction under owner/People guards.
It revalidates current complete sources before making precisely the displayed
operation eligible for recall. It never refreshes original evidence, increases
inference certainty, replaces originals or invents lost legacy attribution.
Rejected proposals cannot be applied. Merge undo and derived rejection/deletion
retain their existing semantics. Turning off reviewed mode stops new proposals and
reviewed recall while preserving owner inspection and undo.

The daily scheduler persists its claim before any provider work, admits at most
one bounded slice per 24 hours, retains failure/empty outcomes, and does not catch
up missed days. Existing USD 0.25 slice and USD 2 daily ceilings, two-call limit and
120-second deadline remain. Pause stops new slices; an in-flight call may finish
within its reservation, but cannot autonomously apply a proposal. Original records
and source admissibility stay unchanged; unavailable evidence produces an honest
blocked/empty result rather than reconstructed input.

## Evaluation and release boundaries

The frozen corpora, observations, human decisions, precision/recall floors and
answer-lift requirements remain unchanged for **unattended** reconsolidation.
Supervised activation admits only operations explicitly approved by the owner;
it cannot expose old unapproved automatic operations when the automatic certificate
is absent. Review feedback is not benchmark truth and approval is not fresh evidence.
ADR-0171's isolated fixture experiment and exact-text consent remain separate.

This is a deliberate amendment to ADR-0169's activation boundary, authorized by the
owner's request for reviewed production use. It is not permission to merge, create
a pull request, deploy, enable unattended application, waive source privacy or
change the autonomous quality gate.

## Verification

Begin with shared in-memory/PostgreSQL failing contracts for pending recall
exclusion and explicit approval. Cover ownership, scope, stale revisions, source
changes and erasure, retry/idempotency conflicts, rollback, historical cutoffs,
approved-only recall, daily concurrent claim/restart, pause, empty/failure history,
HTTP authentication and validation, and browser approval/rejection/undo journeys.
A migration must preserve existing history and refuse downgrade that loses pending
review or scheduling state. Use fabricated sources in tests and UI fixtures.
