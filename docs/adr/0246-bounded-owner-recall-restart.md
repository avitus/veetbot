# ADR-0246: Represent a separately authorized recall restart

- Status: Accepted engineering mechanism within the owner's readiness repair request; activation not authorized
- Date: 2026-10-25
- Related: ADR-0218, ADR-0220, ADR-0223
- Scope: Existing M33 bounded trial; no milestone or quality gate moves

## Context

ADR-0223 allows a restart after a lapse only with a separate explicit owner
response and assessment, without resetting the original seven-day clock.
Version-three admission represents continuous, timely daily renewals only. It
cannot express that separate restart. Treating late feedback as a renewal or
changing the original start would violate the existing contract.

The owner requested compatibility, rollback and end-to-end verification and
repairs needed to prepare semantic recall for reactivation. That request
supports implementing the missing mechanism; it is not a restart decision,
permission to create a pull request, or production activation authorization.

## Decision

Version-four bounded admission/evidence adds one `production_restart` object.
It retains the original `production_trial`, including its start, authorization,
original comparison identities and actual check-ins. Missing feedback remains
missing. The manifest's `activated_at` remains the original start.

The restart binds the same trial, tenant and human owner; a separate explicit
restart response reference, statement hash and instruction; decision and restart
times; the original trial object's canonical SHA-256 and prior evidence hash;
the previous expiry; a fresh assessment and withdrawal receipt; exact current
input digest; and compatible rollback build. Hashes identify operator-retained
evidence and do not authenticate the owner or establish compatibility by
themselves. The operator must verify the actual private receipts.

The previous expiry is recomputed from the preserved original trial. Its
check-ins must still satisfy the original contract. The separate decision must
be at or after that expiry, no later than restart, and neither may be in the
future. This mechanism handles a lapse with no accepted stop or serious-incident
check-in. It does not clear such an incident or override a stop decision.

Initial restart expiry is exactly the earlier of 24 hours after restart and
seven days after the original start. Subsequent genuine check-ins retain all
ADR-0223 rules, but their inventory begins at the restart. Earlier runs and
response references cannot be reused. The inactive interval and unfinished
historical review remain separate evidence; they are not invented active use.
Only one restart phase is represented. Another lapse requires a separate
assessment and decision about further work, not replacing this phase silently.

All existing source, schema, configuration, comparison, owner, evidence-ACL,
local-encoder and interactive-only restrictions remain. Version-one, two and
three admissions retain their contracts and reject restart objects. Source or
evidence mutation, withdrawal, expiry, or a clock before the restart invalidates
the composed handle permanently. Restoring bytes or moving the clock back cannot
revive it. No scheduled/delegated semantic recall or unattended reconsolidation
is enabled.

## Operations and release

The guide describes the separate restart preparation. Refresh the final exact
source and configuration assessment after this repair is reviewed, tested and
delivered through an explicitly authorized release. A current production
readback and compatible application/docs/website rollback are still required.
Never restore a withdrawn historical manifest or replace its retained evidence.
Install a new root-controlled admission only after the actual separate owner
restart instruction and all release and operational gates are satisfied.

No model or encoder evaluation rerun, paid benchmark, synthetic review export,
new trial clock, or automatic activation is part of this change. The original
failed quality gates and cost ledger remain unchanged.

## Verification

`tests/unit/test_bounded_recall_restart.py` first reproduced four failures:
version-four restart decisions were routed to the unsupported legacy binding.
The tests cover restart at expiry and near the seven-day boundary, subsequent
real-use review, preserved earlier reviews, source and evidence revocation,
clock reversal, incomplete/mismatched decisions, duplicate receipts/runs,
missing sample evidence, late or future reviews, stops and serious incidents.
The older bounded-admission and trial suites preserve their existing refusals.
Every authorization and review in these tests is synthetic test data.
