# Approved general recall corpus

The owner approved this 400-scenario corpus for evaluation on 2026-10-13.
The retained authoring folder is not, by itself, measured activation evidence.
`development.jsonl` contains 200 isolated development scenarios, twenty-five per
family, adapted from the first twenty-five information needs in each family of
`general-memory-recall-seeds.v1.tsv`. That diagnostic has already informed tuning;
none of these cases is eligible to become holdout evidence.

Each case supplies its own synthetic owner history, a task, requested output
fields and separately held gold evidence/values. Attribute tasks request proposed
lookup parameters; they perform no lookup or external action. Numerical fields
request digits. Exact-answer aliases are authored data, not model-generated judges.
The original diagnostic corpus is unchanged.

Explicit factual questions remain explicit even when they mention a project or
routine. Six development questions use newly authored choice tasks grounded in
their existing histories; preference configuration fields are proposed task/tool
arguments, not complete dinner suggestions or document drafts. Each family/split
contains at least two implicit tasks, enforced before release scoring. The vehicle
fuel case is marked as a synthetic analogue of the original owner incident. These
labels and proposed task arguments are included in the approved corpus.

The corpus passed structural/provenance validation and the owner's independent
review. It includes 200 newly authored holdout scenarios across all eight families,
explicit two-session temporal
corrections, independently sourced atoms for development multi-fact cases, and
holdout ambiguity/unknown cases. None of the holdout cases has been run against a
model. The accepted independence receipt binds the exact scenarios and corpus digest.

`review.html` is a read-only, searchable review packet containing all 400 cases.
`review-manifest.json` records its corpus digest and approved state. The accepted
receipt is retained in `evals/observations/general-memory-recall/20261013-corpus-review/`.
Separate human review of difficult failures remains required, alongside complete
comparative measurements and release verification. Corpus approval alone does not
satisfy the M33 quality or activation gates.

The original diagnostic and all earlier pilot snapshots remain unchanged. The draft
is not a replacement for a separately measured hard-boundary and load matrix.
