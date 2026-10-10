# ADR-0170: Review semantic equivalence in the reconsolidation benchmark

- Status: Accepted — owner approved on 2026-10-08; offline contract implemented; first bound review completed; quality gate still fails
- Date: 2026-10-08
- Related: ADR-0169
- Governing design: `docs/plan/memory-reconsolidation.md`

Renumbered from ADR-0149 on 2026-10-09, with owner authorization, when the
unpublished M32 work was integrated with dev. The decision and approval dates
are unchanged. Frozen evaluation artifacts retain their original ADR identifiers.

## Problem

The M32 benchmark matches a hypothesis only when its NFC/whitespace-normalized
statement exactly equals a frozen reference and its original-support set matches.
That is reproducible, but it conflates wording with the quality of a novel
connection. A short statement about rail access influencing travel planning can
express the same relationship as a different short statement connecting train
travel with hotel selection. Exact wording gives them different truth labels.

This does not excuse unsupported hypotheses or the failed initial comparison.
That run also exposed real writer and prompt problems, which require fixes and
fresh measurement. Zero false merges, preservation of every original and useful
answer coverage remain independent requirements. A successful provider verifier
is not benchmark ground truth.

## Decision

Keep the existing v1 corpus, manifest, scorer and every recorded result immutable.
Do not retroactively mark either experimental comparison as passing.

For a separately versioned evaluation, retain the frozen reference meanings and
exact support-set requirement. Add a small, offline human-adjudication artifact
for generated hypothesis wording. The reviewer sees randomized candidate identities,
original support and the permitted reference meanings, with model/arm/repeat labels
withheld. Every output receives exactly one decision: equivalent to one existing
reference, not equivalent, or uncertain. Unknown or missing decisions fail the run;
uncertain decisions receive no true-positive credit. The reviewer cannot add a
reference after seeing an output or accept a new fact, person, quantity, polarity,
certainty level, motive or causal assertion absent from that reference.

Bind each decision to hashes of the candidate, complete original support, corpus,
scorer and reviewed observation artifact. Publication recomputes one-to-one matches,
duplicate-output penalties, precision and recall from those decisions. All outputs
remain in the precision denominator and all existing labels in recall. No label
enters the runtime or a provider prompt. This adds no production model, dependency,
background workflow or memory storage mechanism.

The existing thresholds remain: precision at least 0.80 and recall at least 0.60
on each split over three complete repeats, zero false merges/lost facts/boundary
violations, duplicate coverage at least 0.80, no direct-recall regression and at
least 0.10 absolute answer-coverage lift. Answer scoring, budgets, the M16 baseline
and applicable formation/People floors are unchanged. Freeze the revised scoring
contract before another measured run; the currently inspected holdout cannot be
represented as a fresh unseen holdout.

## Approval and limits

The owner explicitly approved ADR-0170 on 2026-10-08. This authorizes the versioned
adjudication contract and its adversarial tests. The v1 scorer and historical
results remain immutable. An owner or explicitly designated human reviewer must
still judge every blinded output; approval of this ADR supplies no judgments.
A failed useful-recall or grounding result remains a failed result after approval.

Automatic token similarity was rejected because a near-identical sentence can
reverse a negation or change a quantity. A new model judge is unnecessary for this
small synthetic corpus and would introduce another source of grading uncertainty.
