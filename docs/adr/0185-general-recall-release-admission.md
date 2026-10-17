# ADR-0185: General recall release admission

- Status: Accepted implementation mechanism under ADR-0178; activation remains gated
- Date: 2026-10-11
- Related: ADR-0178, ADR-0180, ADR-0181, ADR-0182, ADR-0183
- Detailed design: `docs/plan/general-memory-recall.md`

## Decision

Add a closed `general-recall-release@1` observation bundle and an admission verifier.
A claimed passing score, the existing pooled diagnostic, or a candidate-only
semantic experiment cannot supply this bundle. No production composition, provider
activation, new environment setting or change to frozen M16/M32 evidence is made.

The bundle binds the full release revision, implementation and scorer bytes,
source corpus, scenario manifest, reviewed independence assessment, effective
policy/profiles/context budgets/authority rules, embedding configuration (including
price, egress and limits), formation and answer-reader configurations, evaluated
index manifest, preregistration and frozen controls. Every observation and gate
receipt carries the complete dependency digest; mixing versions is rejected.
The caller must independently obtain the active dependencies from trusted
composition and reviewed artifacts, never copy the bundle's claimed identity or
accept model/tool arguments. The binder also computes code/scorer digests locally.

At least 400 independent scenario entries cover all eight families, at least fifty
each. Scenario lineage cannot inflate the count; development and holdout cannot
share scenario/query lineage or source-history digests. Each family has both splits
and enough paired implicit cases to report uncertainty. Scenario entries retain
identifiers, source-history digests, expected evidence and mechanically scored
answer-atom identifiers, expected action and incident/implicit flags. They do not
carry source prose or model-visible answer hints. A reviewed independence digest
is required; structural uniqueness alone cannot establish independent authorship.

Require the complete scenario matrix: automatic and deliberate recall, correctly
seeded and formation-to-answer pipelines, and sparse, dense and hybrid arms.
Missing, duplicated or foreign observations cannot disappear into an aggregate.
Formation, candidate, top-twenty ranking and rendered evidence IDs remain separate,
with subset/uniqueness checks. Answer success requires the expected action and
atoms, available supporting evidence, and no unsupported or invalid use. Correct
abstention and clarification are scored explicitly; guessed atoms do not count.
The runner must derive atoms mechanically after reading and never reveal gold
labels to retrieval, formation or the answer model.

Recompute required-evidence recall and answer success per split, path, pipeline
and family. Hybrid ranked and rendered recall must reach 95% overall and 90% per
family under one preregistered context cap. Both paths' held-out implicit
formation-to-answer results must improve by ten percentage points overall, with
no family losing more than two points on implicit tasks or across all its tasks.
Explicit-question improvement cannot fill the implicit-task requirement. All hybrid known-incident observations must succeed. Any
hard-boundary failure or invalid-current-fact use blocks admission, including in
controls. Report paired normal-approximation 95% intervals, stage recall,
retrieved-but-unused evidence, unsupported answers, noise, p50/p95 latency, tokens
and cost. Intervals are descriptive, not an additional quality threshold.

Preregistration binds a production-shaped baseline report, numerical latency and
cost ceilings and the common context budget, before observation timestamps.
Reject excess cost, searches, rounds or context tokens. Hybrid p95 latency must
stay within the registered ceiling overall and per family. Eleven complete,
passing, exact-tuple gate receipts accompany the observations; gate twelve is
admission itself. Load, privacy, lifecycle, source-recovery and frozen-compatibility
checks remain required in those reviewed reports. The operator trusts the runner,
reviewer and artifact publication process; hashes cannot prove honest measurement,
review, prior registration or receipt provenance. Publishing that evidence from
the real harness and independently authored corpus remains required. Failed input
bundles must be retained; a rejected bundle is never rewritten into passing data.

Admission reads a regular, non-symlink file of at most 16 MiB, recomputes the
comparison and validates the exact active identity. Its callback hashes bounded
bytes rather than accepting a mutable pass flag. Observed replacement, deletion,
or unreadability permanently withdraws that binding; restoring previous bytes
requires explicit revalidation. Existing runtime and indexing admission callbacks
consume this result and retain their cancellation/spend behavior. This is an
internal mechanism with no new public endpoint or scheduled work.

## Verification and remaining work

Behavioral tests first failed against conservative interface stubs, then exposed
mixed-version observation/receipt acceptance and explicit-question masking of
implicit-use failures. Fabricated unit metric fixtures test arithmetic, matrix
coverage, lineage, boundaries, incident use, budgets, version binding, bounded
file access and latched withdrawal. They are not an independent release corpus.
Both recall paths exercise file withdrawal through the run-bound semantic runtime;
query preparation and indexing remain disabled after restoration of the old file.

Production configuration and trusted dependency/authority resolution, query and
index scheduling, actual runner/publication integration, historical/People parity,
task-aware planning/recovery and reviewed held-out observations remain open.
No M33 gate is completed and no running deployment's recall fix is claimed here.

## Offline publication implementation (2026-10-12)

`agent_core.evals.general_recall_publication` compiles a complete version-two bundle
after independently supplied reviews and test reports are present. It requires a
clean committed implementation, the independently resolved deployment identity,
an independence review covering every scenario before preregistration, and a
difficult-failure review bound to all observations. Failures include missing
evidence at any stage as well as unsuccessful answers, across controls and candidate
arms. Reviewer attribution remains an operator responsibility; the compiler never
creates or supplies a human judgment.

Every one of the eleven gate report files must match its receipt, identify the
same dependency tuple and contain distinct named checks whose outcomes recompute
the receipt counts. The compiler recomputes the quality gates, rereads every input
to detect changes during scoring, and rechecks the implementation before creating
a new output directory. Existing and partially written outputs are never replaced.
The admission bundle is written last, after its identity, report and provenance.
Publication changes no production setting and does not itself approve deployment.

The CLI accepts `--bundle`, `--identity`, `--independence-review`,
`--failure-review`, `--collection-manifest`, eleven repeated `--gate-report` paths
and a new `--output` directory. The collection manifest binds the original corpus,
runtime observations, formation replay banks and provider reservation journal.
Paths must stay inside its directory; every input is a bounded regular file with
a matching digest. The collector derives the scenario manifest from the corpus,
requires every scenario/path/pipeline/channel combination exactly once, and
recomputes each observation from original runtime results. Every measurement must
follow preregistration. The canonical bundle must exactly match this compilation.

Cost reconciliation matches every fresh provider completion to a settled original
reservation, retaining repeated requests and enforcing the shared monetary ceiling
throughout the journal. Missing, duplicated, uncertain, unobserved and excess
charges refuse publication. Replayed formation contributes its original usage to
each case, while its actual provider charge occurs once. The compiler verifies
the original bank bytes before accepting replayed usage. The publisher rereads
all source artifacts before writing and records both attributed observation cost
and actual journal cost; neither can replace the other.

These mechanisms do not convert earlier development measurements into release
evidence. A confirmatory runner must still resolve and bind the actual effective
deployment, formation, reader, index and frozen-control dependencies before any
holdout execution. Independent human reviews and actual passing inputs remain
open. The unit collection fixtures deliberately substitute fabricated projections
to test completeness and provenance; runtime projection has separate execution
tests. Neither fixture supplies a human review or activation evidence.
