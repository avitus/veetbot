# ADR-0168: Bounded browser scrolling without approval

- Status: Accepted under the owner's implementation instruction
- Date: 2026-10-09
- Amends: ADR-0058 decisions 2 and 11, browser action approval policy
- Scope: Existing authenticated browser tools, including scheduled runs

## Context

The morning X briefing reached the authenticated feed, then parked on approval
for a scroll. Approval arrived after the five-minute run deadline, so the run
cancelled without a summary. The owner explicitly authorized unattended scrolling
and asked to reduce excessive approvals generally.

## Decision

The default policy adds an exact `browser.act` rule with the
`bounded_browser_scroll` condition: allow a validated scroll of 1–2,000 pixels
in either direction through the isolated browser provider; otherwise require
approval. The condition accepts only the existing revision, reference, delta
and optional observation postcondition fields. It rejects coercible non-integer
deltas, unknown fields, other action kinds and mismatched tool classifications.
It applies to scheduled and interactive runs without creating a grant.

The tool retains its conservative external-write classification, serial
execution, effect watermark and non-idempotent recovery. Scrolling can trigger
site JavaScript and network requests; this is explicit permission for that
bounded gesture, not a claim that it cannot change website state. Existing
profile, origin, revision, authentication, egress and run-budget checks remain.
No uncertain scroll is automatically replayed.

Hardline rules still run first. The condition is opt-in in a versioned policy
profile, so a profile without it keeps asking and an explicit deny still denies.
Only a successfully matched scroll condition exempts page-derived argument
references from the external-untrusted argument overlay. The proposing turn
must still have platform, trusted-configuration or user authority. The pipeline
supplies the newest user message's trust separately from the turn's accumulated
tool-result taint; observing a page does not remove the owner's scroll permission,
and an untrusted newer message cannot borrow an older owner's authority. The rule
does not claim human confirmation and cannot cover clicks, typing, keys,
selection, uploads, sends, publication, deletion or account changes.

The policy profile hash changes. Existing approvals and grants retain ordinary
policy-version revalidation. Tool schemas and versions remain compatible with
already pinned schedules. No milestone or acceptance gate changes.

## Verification

Begin with a failing regression for a scheduled, profile-bound run that
navigates, scrolls and synthesizes without an approval. Cover interactive runs,
malformed scrolls, untrusted origins, page-derived references, hardline and
operator denies, unchanged consequential actions, stale references and uncertain
dispatch. Run the policy/browser partitions and final sidecar `make check`.

The initial regression failed at `WAITING_FOR_APPROVAL` in both scheduled and
interactive hosted runs. After the repair, 179 focused policy, provider-tool,
composition, schedule-binding and regression checks passed on the sidecar.
Production acceptance remains a delivered run reaching its summary without
a scroll approval; no live schedule or authority record was changed in development.

## Memory evidence repair

The policy-version change required fresh memory activation evidence. The first
three-repeat comparison failed the existing represented-clause gate and measured
holdout direct recall of 0.933 against the unchanged 0.95 floor. The owner
authorized repairing the current formation policy and explicitly rejected a
temporary fallback. A diagnostic development-case run reproduced a compound
anticipation joining two separately stored facts: neither cited memory asserted
the entire prediction, so the existing verifier correctly refused it.

Anticipation now emits one referenced memory per prediction, enforced by the
response schema, and is instructed to copy that memory's statement. Independent
facts use independent predictions. The verifier, causal blinding, call budget,
scorer, frozen corpora and publication thresholds are unchanged. The regression
first failed because a compound prediction with two memory identifiers was
accepted; the repair passes the focused memory suite. Fresh release evidence
must still pass before the policy change is submitted to dev.
