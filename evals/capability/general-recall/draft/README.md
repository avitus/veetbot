# General recall corpus authoring draft

This folder is an authoring workspace, not a release corpus or activation evidence.
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
labels and all proposed task arguments still require independent review.

The draft has passed structural/provenance validation only. It includes 200 newly
authored holdout candidates across all eight families, explicit two-session temporal
corrections, independently sourced atoms for development multi-fact cases, and
holdout ambiguity/unknown cases. None of the holdout cases has been run against a
model. Scenario and query-lineage labels are proposals, not an independence certificate.

`review.html` is a read-only, searchable review packet containing all 400 cases.
`review-manifest.json` records its corpus digest and explicit unreviewed state.
The reviewer must check distinct information needs, cross-split template lineage,
source and answer correctness, lifecycle labels and acceptable exact-value aliases.
Independent human review and a separate review of difficult pilot failures remain
required. Do not publish release receipts or use this draft to satisfy M33 gates.

The original diagnostic and all earlier pilot snapshots remain unchanged. The draft
is not a replacement for a separately measured hard-boundary and load matrix.
