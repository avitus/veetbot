---
title: General Memory Search and Recall
status: design
canonical: true
---

# General memory search and recall

Milestone 33 is the owner-authorized October 11, 2026 extension of the
[retrieval design](memory-retrieval-and-ranking.md), recorded in
[ADR-0178](../adr/0178-general-memory-recall.md). It admits measured semantic
retrieval and bounded supporting-source recovery from roadmap B6. It leaves
formation, People identity, persona authority and unattended reconsolidation
under their existing contracts.

## Research basis

Primary literature reviewed through October 11, 2026 informs hypotheses rather
than importing another system's leaderboard result as a release certificate.

- [LongMemEval, ICLR 2025](https://proceedings.iclr.cc/paper_files/paper/2025/file/d813d324dbf0598bbdc9c8e79740ed01-Paper-Conference.pdf)
  separates indexing, retrieval and reading, compares conversation granularity,
  and evaluates fact-enriched keys and temporal query expansion. Preserve source
  context and measure answer use separately from evidence recall.
- [Grounding Agent Memory in Contextual Intent, Findings ACL 2026](https://aclanthology.org/2026.findings-acl.584/)
  motivates task-context signals beyond entity or word overlap. Its label-selection
  error analysis motivates fallbacks instead of hard filtering by inferred intent.
- [Hindsight, ACL 2026 demonstrations](https://aclanthology.org/2026.acl-demo.27/)
  demonstrates complementary retrieval channels and reranking. Veetbot must
  measure each channel's contribution; this does not mandate a graph database.
- [Mem2ActBench, ACL 2026](https://aclanthology.org/2026.acl-long.370/)
  measures implicit memory use and distinguishes retrieval misses from unused
  evidence and incorrect tool arguments. Include each failure stage in evaluation.
- [Memora, Findings ACL 2026](https://aclanthology.org/2026.findings-acl.1337/)
  motivates separately penalizing reliance on invalidated memories. Retain the
  repository's mechanically scored gates rather than adopting its model judges.
- [RIME, September 28, 2026 preprint](https://arxiv.org/abs/2609.34438)
  motivates a bounded source recovery experiment when compressed memory lacks
  detail. [Experience-Amortized Reranking, August 2026 preprint](https://arxiv.org/abs/2608.22767)
  motivates later reuse of relevance judgments with source-version invalidation.
  Neither preprint justifies immediate activation or a learned production policy.

## Scope and implementation order

1. Freeze a diagnostic control, define the corpus/scorer and record per-stage
   baseline measurements. Register gates before production behavior moves.
2. Repair category-independent candidate selection, first with failing shared
   contracts and then with both persistence adapters and runtime regressions.
3. Evaluate lexical/structured, semantic-only and hybrid retrieval with identical
   inputs and output budgets. Choose an embedding tuple from evidence, not from
   a hard-coded model or object dictionary. Admit pgvector only on material lift.
4. Evaluate task-context query formation and a bounded second retrieval round,
   then reranking. Attribute gains and costs independently before combining them.
5. Add governed evidence packages and bounded source expansion. Verify use in
   answers and proposed tool arguments, including omission and ambiguity cases.
6. Publish version-bound comparative evidence; activate only passing features,
   retain rollback, and remove the ownership branch when its regressions pass
   without it. PR creation, merging and deployment keep their existing authority.

No incomplete stage is reported as a general fix. The first repair addresses
candidate starvation; it does not solve implicit semantic information needs.

## Candidate contract

Hard predicates precede all candidate limits: tenant, principal, erasure fences,
current and historical sensitivity, lifecycle, validity, explicit type filters,
local portability and explicit exclusions. New retrieval methods cannot widen
these predicates. Historical selection chooses a revision before computing its
relevance, while current restrictions still apply to historical content.
Historical merge support validation may read persona-excluded originals under
those same visibility rules; that internal lookup never adds them to candidates
or rendered context. Exclusion remains mandatory for returned evidence.

For a task query, retain independently bounded lexical and structured candidate
lists. Lexical selection ranks by query-term coverage before chronology;
structured selection ranks exact subjects/identifiers before broad type anchors.
An ownership anchor remains only as a compatibility path during the transition.
Each arm retains at most `max(max_items * 8, 64)` candidates; deduplication of
their union bounds the two-arm result to twice that limit. Rank ties are stable
by store position and identifier. Query-term repetitions have no extra vote.
Snapshots and deltas without query anchors retain their existing chronology.
No query-less arm scans the entire personal profile into the task context.

The initial repair uses identical whole-lexeme coverage arithmetic in both
adapters. It is a transparent baseline for later FTS weighting and semantic
comparisons, not a claim that term coverage is the final relevance function.
SQL computes relevance before LIMIT; selecting a recent page and sorting that
page in application code does not satisfy this contract.

An explicit legacy mode preserves the old bounded newest-first selection for
frozen experiments. The effective selection mode and retrieval policy are recorded
in the trace. Existing saved queries that lack a selector retain legacy candidate ordering.
New live queries use relevance selection. Ordinary memory browsing is unchanged.

The semantic relevance path clears the older possession-specific candidate and
ranking flag before both query preparation and retrieval. Automatic and deliberate
car/laptop regressions recover the older provisional model amid newer distractors
without that branch or snapshot promotion. This tests retrieval with controlled
vectors; the confirmatory corpus must still establish actual encoder and reader
quality. Sparse compatibility and explicit legacy controls retain their historical
policy, including when semantic recall is disabled.

## Query formation and search

The planner receives the current task, unresolved references, active entities,
open questions and current message. It produces a bounded set of information
needs and searches. It receives neither gold answers nor relevant-memory labels.
An initial experimental bound is four searches and two total retrieval rounds;
one shared deadline and token/cost budget apply across rounds and channels.

The deterministic path makes no new model call. Assisted planning must use a
revision-bound, principal/scope/surface-aware cache or the slower escalation path.
Cache keys include task context and all policy/model versions; caching an earlier
answer is not caching a query plan. A changed source, permission or erasure fence
cannot reuse cached evidence. Cancellation and uncertain usage retain reservations.

[ADR-0193](../adr/0193-bounded-task-context-query.md) adds a default-off deterministic
task-context ablation. It composes one query from the current request and bounded
working-state fragments, excluding completed tasks and retaining whole fragments
within 512 UTF-8 bytes. An oversized current request retains the baseline path;
the actual embedding token limit still applies. It adds no model call, cached
evidence or inferred temporal filter. Current release admission is required, and
preparation and context assembly share the same former. Assisted planning,
second-round comparisons remain open.

[ADR-0195](../adr/0195-shared-recall-context-budget.md) shares the twenty-item,
two-thousand-token allowance across retained deliberate results and automatic
base/delta recall in each model request. At most four whole search blocks enter
current-source validation under one shared second; unknown or changed evidence is
withheld. Old search results never confer their own continued visibility. The
frozen prefix remains its existing separate class, and correction notices remain
mandatory when optional recall has exhausted its allowance. Preparation and
assembly compute the same current remainder. This bounds rendered context; it
does not by itself certify a whole-run search-work cap or a combined global
relevance rank. A missing retained trace, unavailable source recovery or
unavailable read authority invalidates only that result; later valid receipts
still receive their own validation, and only semantic traces consult authority. If the correction store is
unavailable after optional search exhaustion, context assembly applies the same
logged degradation as its other dependency-failure paths and keeps the explicit
exhaustion notice. Cancellation still propagates.

[ADR-0196](../adr/0196-run-recall-execution-budget.md) enforces the durable
reservation during actual M33 search execution, including sparse fallback and
People discovery. Automatic rounds reserve their base and possible delta;
deliberate rounds reserve one slot per validated search. All execute under the
same deadline and four-slot/two-round ceiling. Exhaustion supplies an explicit
limitation while retaining mandatory corrections. Unused and failed reservations
cannot be renewed after resume. The runtime evaluator uses this same allowance
for sparse and semantic arms. Context assembly now records its existing block
priority as one deduplicated twenty-original rank prefix, as specified in
[ADR-0192](../adr/0192-recovered-source-evaluation.md). The scorer verifies that
runtime receipt against exact observed blocks; it never invents a cross-query
order. A missing receipt leaves different per-search orders uncertifiable.

[ADR-0206](../adr/0206-session-default-memory-search.md) makes the planned tool's
project scope optional. Ordinary searches use the authenticated session project;
an explicit scope retains cross-project lookup. Preparation and execution resolve
the same default. No model must guess a project from a task topic, and no default
changes confidence, portability or source-recovery restrictions.

Intent and inferred dates can boost matches but cannot become mandatory filters
without an explicit user constraint and a tested fallback. Failure to find a
memory is not proof that the underlying fact is false. A second search targets
missing evidence; it cannot invent a value to make an answer complete.

Planned `memory.search` defaults to the authenticated session's project and
current knowledge. Its schema directs the planner to omit temporal filters for
ordinary current recall, rather than inventing dates. Explicit requested scope,
valid-time and recorded-knowledge constraints remain hard filters. Planned search
shares the People context and retained-dependency checks in
[ADR-0208](../adr/0208-grounded-relationship-discovery.md).

## Semantic experiment and index contract

Use a provider-neutral embedding seam with bounded batches, explicit model and
dimension, finite vectors, verified output ordering and usage. No silent retries,
unpriced sends or implicit provider selection. Production egress requires the
configured residency and sensitivity policy before source serialization. Begin
offline with synthetic inputs and a fixed monetary ceiling.

Index entries identify the principal, source belief/revision, embedding tuple,
derived-text version and content digest. The authoritative belief remains the
source of truth. Derived aliases and contextual labels are navigation metadata,
not asserted memory, corroboration or refreshed evidence. Backfill is bounded,
resumable and fair to old records. Corrections and deletion fence entries before
asynchronous cleanup; a stale backfill job cannot restore them. Retrieval rejoins
current permitted originals before ranking or rendering any candidate.

Compare sparse-only, dense-only and fusion, with unchanged final item/token caps.
Use the current design's reciprocal-rank fusion as the first hybrid baseline;
test its contribution rather than assuming it wins. Keep separate channels for
beliefs and source episodes, avoiding multiple derived keys counting as independent
evidence. A reranker runs on already permitted candidates and has its own ablation.

The retrieval profile names `semantic_channel: hybrid` by default. The explicit
`dense` comparison omits sparse candidates and sparse ranking signals when a
prepared semantic query is available, while retaining all visibility, lifecycle,
trust, rendering and output-budget rules. Its trace identifies the dense channel.
An unavailable semantic query retains the existing sparse fallback; evaluators
must record that fallback and cannot count it as a dense observation. The complete
profile remains part of release identity, and semantic retrieval remains disabled
by default. [ADR-0191](../adr/0191-experimental-relevance-reranking.md) adds a
separately admitted, default-off relevance reranking ablation after fusion. It
keeps uncertainty labels and hard predicates while measuring stronger relevance
against the metadata-weighted baseline across semantic, linked and lexical inputs.

## Local semantic index implementation

[ADR-0179](../adr/0179-versioned-semantic-index.md) implements the derived-index
boundary after the measured pilot. `SemanticMemoryIndex` captures version-bound
originals, compare-and-swaps computed vectors, and supplies bounded immutable-order
inventory and change pages. It does not serialize sources to a provider or schedule
backfill. `memory_vectors` uses pgvector exact cosine search, owner-bound foreign
keys and forced tenant RLS. Both adapters remove vectors transactionally on source
changes and rejoin current permitted originals before the result limit. Changes to
usage alone do not re-embed content. Separately indexed historical revisions use
the current original's invalidation fence under
[ADR-0200](../adr/0200-historical-semantic-revisions.md). Dated queries select the
authoritative revision and its restrictions before similarity and candidate limits.
Current vectors never stand in for past evidence. Historical indexing is an
explicit, default-off local configuration; it grants no remote export authority.
The disabled default preserves the current-only configuration digest used by
existing jobs, reservations and deployment manifests. An explicitly enabled
historical arm has a distinct digest and retains the normal inventory reset rule.

The experimental `SemanticQueryLookup` is a bounded local lookup of an exact-query
hash and explicit embedding tuple. It holds query vectors rather than cached
source content; originals are resolved on every recall. The shared retriever
admits semantic candidates under the same final item/token caps, lifecycle weights,
trust scan and rendering. Semantic RRF order follows cosine rank. Verified equivalence transfers semantic
relevance to permitted originals before scoring, so a partially indexed group
still cites its canonical fact and contributes only one fused result. Trace policy
records `semantic@1` and the embedding-space digest. The embedding tuple includes
provider, model, model revision, dimensions and `memory-text@1` (original subject
and statement, with no inferred labels).

The shipped configuration supplies no lookup and `semantic_enabled` remains false. A populated
index alone cannot activate a provider or change production recall. The bounded
indexing service now implements explicit provider admission, durable reservations
and resumable inventory under [ADR-0180](../adr/0180-bounded-semantic-indexing.md).
Its optional maintenance composition is described below. Explicit bounded query preparation now populates
that local lookup under [ADR-0181](../adr/0181-bounded-semantic-query-preparation.md).
It accepts at most four queries, classifies text with an explicit sensitivity floor,
reserves one request against the same durable owner/day budget as indexing, and
makes the provider call outside repository transactions. Lookup itself never calls
a provider or enqueues work. The bounded five-minute cache binds the complete query,
current policy, surface, task and embedding configuration; it holds no source evidence.
[ADR-0182](../adr/0182-run-bound-semantic-recall.md) adds a run-bound lookup to the
shared retriever. Explicit preparation and recall normalize the same effective
query, including the current merge policy. Binding reads the authorized run,
session and saved checkpoint, requires a live lease and trusted policy/surface/
sensitivity authority, and hashes the complete saved task context. Separate chat
handles share one 128-vector cache; state changes, cancellation and eviction revoke
their handles without clearing another chat's valid vectors. Ordinary recall
performs no provider work. Binding is limited to one second; the ten-second
preparation budget includes state reads, with up to three seconds of failure cleanup.
[ADR-0183](../adr/0183-fresh-context-recall.md) removes completed evidence reuse
from production context assembly. Every assembly reaches current-source recall,
including after query preparation, expiry or admission withdrawal, while the frozen
prefix remains unchanged. The runtime already uses one atomic assembly for pressure
and the model request. A complete dependency revision can explicitly opt other
callers into reuse; absent or unavailable validation always reads fresh evidence.
[ADR-0185](../adr/0185-general-recall-release-admission.md) adds the release-bundle
verifier described below. [ADR-0187](../adr/0187-semantic-index-production-composition.md)
connects source indexing to maintenance with explicit configuration and matching
release evidence. It ships disabled, starts only without live foreground leases,
and cancels/joins bounded work on shutdown. It independently binds effective
composition and embedding configuration to a reviewed manifest before accessing
credentials; either evidence file changing permanently withdraws admission.
[ADR-0188](../adr/0188-local-semantic-embeddings.md) adds an optional offline CPU
adapter with verified model/tokenizer bytes, bounded native work and no runtime
download. Restricted sources and tasks stay local under their existing labels.
With complete passing evidence, the local composition binds the actual reader,
owner, current checkpoint and live private surface, then explicitly prepares the
same queries used by context assembly and deliberate search. Runtime preparation
saves that checkpoint first, runs outside transactions and remains separately
bounded; a miss in ordinary recall never schedules it. Remote query provenance
and complete release observations remain open.

[ADR-0197](../adr/0197-people-semantic-query-narrowing.md) permits current People
discovery to reuse a prepared vector only when the actual encoder bytes match and
every hard query restriction is unchanged or narrowed. The shared cache stores the
validated parent query within its existing 128-entry ceiling; derived lookups add
no provider work or entries and retain the parent's expiry and live run authority.
Current identity restrictions now precede sparse and dense candidate limits in
both stores, including unresolved and hidden links. Explicit People lookup also
retains the caller's included-ID bound. Shared PostgreSQL contracts verify this mechanism; its comparative release
evidence remains pending.
[ADR-0190](../adr/0190-run-recall-work-budget.md) separates local planning from
provider preparation and saves a nonrefundable four-query/two-round allowance with
one ten-second deadline for the run. Checkpoint events preserve that content-free
reservation after materialized checkpoint loss. Runtime handles share the deadline;
source expansion requires the exact prepared query. This bounds optional local
preparation; ADR-0196 additionally bounds executed searches. Whole-request planning
and confirmatory operational evidence remain open. Frozen M16/M32 controls continue
to select legacy candidates without
the semantic arm. The complete M33 gates remain pending.

## Evidence packages and recovery

A selected item carries its original statement, source identifiers, relevant time
interval, derivation, authority and uncertainty. Stable rendering remains data at
memory trust; instruction-like content is blocked by the existing scan. Prefix
snapshots stay frozen; new evidence belongs in the turn context.

Expansion starts from permitted original evidence, with bounded turns/episodes,
bytes and time. A broader source search is a separate evaluated channel; it
cannot bypass a missing belief by returning a deleted or rejected equivalent from
old dialogue. Unknown source visibility withholds content. Current restrictions
and durable corrections apply even when asking about the past. Archival search
remains deliberate, and neither expansion nor a miss triggers re-extraction.

[ADR-0189](../adr/0189-governed-memory-source-recovery.md) implements optional
source recovery with ingestion-stamped visibility, exact-text digests, shared
message correction fences and generated-copy erasure lineage in both stores.
It admits only bounded native owner excerpts within the current project, private
surface and restricted clearance. Quotes consume the existing context budget;
failed source reads roll back before ordinary recall retries. The release-bound
local reader composition can supply this channel, disabled by default. Historical,
People-scoped, frozen and delegated source expansion remain withheld. These
contracts do not constitute a passing quality ablation or activation receipt.

The reader can distinguish missing, conflicting and sufficient evidence. Existing
facts should reach an answer without asking the owner to repeat them. Genuine
unresolved ambiguity requires clarification, not a guessed personal fact.

## Evaluation contract

The release corpus has at least 400 independently authored scenarios and at least
50 in each family: attributes/resources, preferences/constraints, relationships,
goals/projects, decisions/rationale, routines, temporal updates, and multi-fact/
ambiguity/unknowns. A scenario is a distinct information need with its own source
history and expected evidence; renaming an entity or adding distractors does not
create another independent scenario. Paraphrases and load variants are reported
separately. Development and holdout split by scenario and query-template lineage.

Fixtures contain fabricated information. Existing owner incidents may be represented
by synthetic analogues; real-data export retains the separate consent rules.
Public datasets remain operator-supplied, versioned and license-aware, never vendored.
The scorer never supplies expected labels, categories or answer values to retrieval.

Every scenario exercises automatic and deliberate recall. Under
[ADR-0212](../adr/0212-checkpointed-memory-confirmation.md), the required production
comparison records the full formation-to-answer path with sparse control and final
hybrid retrieval: 1,600 observations across the 400 reviewed scenarios. Correctly
seeded retrieval and dense-only comparisons remain separately reported diagnostics;
the legacy release formats continue to require all twelve variants. Include old relevant facts,
newer unrelated facts, hard semantic distractors, another person's facts, conflicts,
negation, updates, deletion, historical questions and information never supplied.
Load tests use 100, 1,000 and 10,000 competing records, with a separately measured
100,000-record PostgreSQL experiment before production scaling claims.

Score stages separately: formed, candidate selected, ranked, rendered and used.
Use deterministic identifiers and fixed clocks; count exact evidence IDs before
rendering and mechanically labeled values/constraints in answers or proposed tool
arguments. Tool-use evaluations do not perform real external actions. Report
retrieved-but-unused facts, hallucinated defaults, stale use and false transfer.
The shared retriever supplies transient candidate, dense-order and ranked IDs
on `RecallResult.stages` before the context budget is applied. These IDs describe
the actual permitted read, avoiding an evaluator's later source query or guessed
ranking. They are excluded from result serialization and model-visible rendering;
only the evaluator retains them in its separately governed observation artifact.
Admitted source reads retain separate content-free provenance before excerpt
budgeting. Exact authored source digests and actual reader visibility determine
source-supported evidence; they never manufacture a formed atom. The source
stage and measured unlabeled noise are specified in
[ADR-0192](../adr/0192-recovered-source-evaluation.md).
Independent human review of difficult failures complements, but never replaces,
the mechanical release scorer.

The targets approved by the owner are:

- All known-incident regressions pass through automatic and deliberate recall.
- Required-evidence recall within the top 20 is at least 95% overall and 90% in
  every family, at the same final context-token budget as the control.
- Held-out implicit task success improves by at least ten percentage points over
  the current control. Publish paired confidence intervals and category results;
  any category losing more than two percentage points blocks activation.
- Zero hard-boundary failures or invalid-current-fact use in the boundary corpus.
- Publish noise, p50/p95 latency, input/output tokens and monetary cost. Before
  tuning, record numerical latency/cost ceilings from the production-shaped
  baseline in the experiment manifest; missing ceilings prevent publication.

The 400-case corpus, semantic lift, implicit task lift and operational budgets are
release gates, not prerequisites to landing an independently verified candidate
starvation repair. That repair must not be advertised as completing this milestone.

## Frozen controls and evidence

Do not overwrite the M16 benchmark corpus/baseline or M32 manifests, scorers,
observations and human decisions. Preserve the old retrieval path for their
reproduction. New experiments name their exact corpus, scorer, implementation,
candidate selection, model, embedding dimension, index state, budgets and seed.
Retain failed runs; never resample only failures and publish the favorable subset.
Publication recomputes gates from observations and refuses missing or mixed tuples.
Synthetic offline retrieval lift does not establish live answer lift.

The confirmation job registers a finite whole-job deadline separately from its
per-answer latency ceiling. Its default remains one day; an explicit deadline
may extend to seven days so the complete 4,800-observation matrix can include
fixture construction and native indexing. The first retained run's median
inter-observation interval projects roughly 113 hours, beyond the earlier
three-day implementation limit; this small-sample projection is not a completion
guarantee. A longer job deadline changes neither the model-spend allowance nor
the production recall deadline or answer-latency acceptance gate. Expiration
still fails the run and retains its original observations and settled/held costs.

[ADR-0207](../adr/0207-bounded-confirmation-concurrency.md) permits an explicitly
registered second scenario worker with a separate guarded scratch database and
native encoder. Both workers share one provider reservation ledger and allowance;
their schema and actual runtime captures must match before held-out task parsing.
The version-two execution manifest binds this choice without rewriting older
manifests. Each scenario still executes all twelve variants in order with its
exclusive paired formation bank. Failures stop new work while already admitted
requests settle. Setup, native indexing and actual reader runtime are reported
separately; contention remains part of measured answer latency. No corpus, load,
quality, monetary or per-answer latency requirement changes.

ADR-0212 supersedes the full-matrix and no-reuse requirements for the separately
identified checkpointed continuation. It preserves every original failed run,
retains completed rows without retagging or outcome-based selection, and requires
byte verification, original formation banks, mechanical reconstruction and complete
ledger reconciliation before reuse. Durable starts and results prevent duplicate
sends after an interruption. An uncertain started answer cannot be resent. One
cancelled formation preparation without any completed answer may receive one
recorded replacement bank; all charges and uncertainty remain accounted for.
Publication of this reduced comparison requires a new explicit protocol and its
verified admission implementation. Existing version-one/two publication does not
accept a reduced matrix. No activation gate is satisfied by checkpoint retention.

[ADR-0217](../adr/0217-bounded-interruption-recovery.md) adds two owner-approved
exceptions bound to retained interruption evidence: one final preparation for
`dev-decisions-03` and one replacement of the admission-halted deliberate/hybrid
reader for `dev-decisions-04`. Original failed records, completed rows, uncertain
charges and the absolute deadline remain unchanged. A separately pinned successor
collector appends the authorization and protocol ancestry; it uses fresh workers
and a distinct final preparation bank. Either exception failing again stops further
paid admissions. This does not authorize retries for other readers or preparations,
change evaluated runtime inputs or relax any quality/security/per-answer limit.

[ADR-0214](../adr/0214-formation-deadline-repair.md) subsequently authorizes a
root-cause runtime repair: formation@11 has a finite 600-second enclosing stage
deadline, separate from the unchanged 120-second transport request. Explicit
caller limits and frozen controls remain unchanged. A pinned source comparison
and complete request regression restrict compatibility to this deadline and its
safe audit metadata. The original 413 rows remain byte-identical; new rows name
a distinct repaired execution identity alongside the original comparison inputs.
Collection/publication version two requires the exact repair manifest, complete
protocol ancestry and independent human review of this exception. It cannot
publish under the unchanged-runtime version or erase the patch from eventual
deployment binding. Only the repeatedly interrupted pre-answer preparation may
start a new bank; completed or unknown reader work cannot resend. All prior
reservations, the original absolute deadline and every quality gate remain.
The owner permits an aggregate cap up to USD 500 if necessary; any added allowance
requires fresh reconciliation, and the original allocation is not renewed.
Independent human review must include both failed attempts and replacements.

[ADR-0215](../adr/0215-formation-terminal-outcomes.md) repairs collector cleanup
after a settled call: slot context cannot span consumer yields, terminal facts
are retained before downstream validation, and cleanup cannot mask the original
failure. An explicitly admitted successor preserves all 418 completed rows,
derives the five missing report entries only from original events and bytes, and
keeps missing wall timings null. A supplemental seal accounts for the stopped
journal without rewriting it or claiming successful original sealing. The failed
pre-answer preparation and proved-unsent reader receive distinct, one-use recovery
events; unknown sent work remains blocked. The changed bank and collector modules
are pinned separately in protocol version four, alongside the existing deadline
repair and all predecessor inputs. This grants no automatic successor or activation.

## Release admission mechanism

`general-recall-release@1` binds reviewed scenario/independence manifests, exact
implementation/scorer/model/configuration/index versions, frozen controls and
preregistered budgets. Every observation and gate receipt binds that same tuple.
The verifier requires all scenarios across both recall paths, seeded and full
formation pipelines, and sparse/dense/hybrid comparisons. It recomputes stage
recall and mechanically observed answer success rather than trusting a pass flag.
Top-twenty ranked and rendered recall use the same context cap. Explicit-question
success cannot supply the held-out implicit answer-lift requirement. Correct
clarification/abstention, unsupported answers, boundary failures, invalid current
facts, known incidents and family regressions remain distinct outcomes. Paired
normal-approximation confidence intervals, noise, latency, tokens and cost are
reported. Preregistration and the eleven supporting gate receipts are mandatory.

`general-recall-release@2` additionally binds authored source identities and observed
source witnesses. Required-evidence recall uses the union of atomic and exact-source
support, while reporting formation and atomic ranking separately. Every required
source for a claim must match, and source-supported top-twenty recall requires an
original ranked within twenty. Rendering requires the excerpt to reach the actual
reader. Version two requires measured noise counts including unlabeled distractors;
version one rejects populated version-two accounting. All release thresholds,
boundaries, reviewed receipts and preregistration requirements remain unchanged.

The production caller must independently resolve the active dependency tuple.
[ADR-0199](../adr/0199-recall-evidence-release-binding.md), approved by the owner,
permits a separate deployment receipt for a different commit only when the original
measurement committed the complete evaluated input manifest and independent Git-tree
and deployed-file checks establish equality. Original observations and reviews keep
their identity. Migration, deployment, packaging, dependency, corpus, effective
configuration or model changes require matching new evidence. The default remains
exact-commit admission, and observed receipt or input mutation latches withdrawal.
The binder additionally checks local source/scorer bytes and reads only bounded
regular files. Observed evidence changes permanently revoke a binding; restoring
old bytes does not revive it. As [ADR-0185](../adr/0185-general-recall-release-admission.md)
explains, the operator still trusts the runner, reviewer and publication process:
hash equality does not establish authorship, honest instrumentation or complete
receipt provenance. The offline publisher now checks independent review files,
complete named gate reports, the clean committed build and unchanged input bytes
before writing a recomputed version-two bundle to a new directory. Its collection
compiler requires the complete raw observation matrix, recomputes evidence and
answer stages, verifies replayed usage against original banks and reconciles every
fresh charge with the settled provider journal. The publisher rejects any canonical
row that differs from those original observations and rechecks source bytes before
writing. It never authors reviews or enables deployment. The confirmatory runner
now freezes deployment composition, verified model assets, actual schema, controls
and runtime variants before held-out parsing. Production startup binds the same
schema fingerprint. Its initial USD 100 aggregate allowance was approved by the
owner and explicitly raised to USD 300 on 2026-10-13, including earlier charges. On 2026-10-13 the owner approved the exact 400-scenario
corpus; its accepted
independence receipt is retained under
`evals/observations/general-memory-recall/20261013-corpus-review/`.
The first confirmation stopped after 34 observations because a necessary integration
fixture correction changes the ADR-0199 input manifest. The original failed attempt,
its complete input capture and settled cost journal remain under
`evals/observations/general-memory-recall/20261013-confirmation-v1/`. The owner
authorized one replacement with a USD 290 allocation after final review and CI.
That replacement passed its review/CI prerequisites on `7214a8df` but stopped at
4 of 4,800 observations. The deliberate arm disabled automatic context while still
reserving automatic preparation; a slow reader response exhausted that unused
reservation before its search. The evaluator now suppresses both automatic context
and automatic preparation in deliberate arms. Validated deliberate calls retain
the same durable reservation, ten-second deadline and execution limits. This is
an evaluation-isolation repair, not a production budget change. All originals
remain under `20261014-confirmation-v2/`; aggregate reported and held cost is
USD 5.62063000. No further paid attempt is authorized by the earlier one-run decision.
The subsequent v3 confirmation on reviewed/verified `74b14815` failed after
1,217 of 4,800 observations. Its original archive and scratch database remain
retained. A mandatory development incident failed in formation/deliberate hybrid
recall despite rendered evidence; the terminal provider cause is unproven because
the budget wrapper could mask its structured failure. Aggregate reported and held
cost is USD 55.913546. On 2026-10-15 the owner requested completion of these gates
before merging PR #163. ADR-0206 records the scope and diagnostic repairs; a fresh
complete run is limited to USD 240 after ledger reconciliation and final review/CI.
The failed run and earlier load measurements cannot certify changed inputs.
The subsequent two-worker v4 on `f55c597c` stopped after 771 observations when
the normalized-stream credential detector rejected an event. The triggering
field was not retained, so a real credential versus an opaque-continuation
false positive remains unproven. All 139 result files, both scratch databases
and supervision originals are retained; cumulative reported and held cost is
USD 83.96363625, leaving USD 216.03636375 under the unchanged aggregate cap.
All four observed hybrid incident checks pass, but this development-only prefix
cannot establish full quality or held-out lift. The canonical compiler rejects
37 relationship rows as semantic fallback. A zero-provider reproduction found
that the evaluator substituted measured recall but left People semantic binding
on the original retriever. Binding must follow the same measured instance; the
compiler and semantic-channel requirements remain unchanged. Neither this repair
nor diagnostic improvements authorize reusing the failed attempt's observations.
V5 on `f837ed3c` subsequently stopped at 124 observations. Its bounded diagnostic
identifies the provider-key detector in OpenAI `reasoning.encrypted_content`,
but the original match was not retained and its meaning remains unproven. All
31 result files, both scratch databases and supervision originals are retained.
Seven original ledgers total USD 88.371792, including USD 5.64272 uncertain
reservations, leaving USD 211.628208 under the unchanged USD 300 aggregate cap.
On 2026-10-16 the owner approved the narrow OpenAI repair in
[ADR-0210](../adr/0210-openai-opaque-credential-boundary.md). Only embedded
provider-key collisions in the schema-bound opaque value may pass; standalone
secrets and all other fields/providers retain rejection. Deterministic stream and
actual adapter/runtime replay regressions precede fresh reviewed source, native
measurement and complete confirmation. No failed rows are reused, no quality
gate is weakened, and neither this prefix nor native load establishes full quality.
That repair's subsequent v6 run on `16153a5f` stopped at 1,239 observations after
a cancelled formation call. Its complete originals and both scratch databases are
retained. Eight ledgers total USD 135.90882425 including USD 7.83424 in uncertain
reservations, leaving USD 164.09117575 under the unchanged cap. The owner then
approved checkpointed continuation (ADR-0212), replacing the earlier no-reuse
method for unchanged, verified inputs. Work now proceeds on the isolated
`codex/memory-retrieval-evaluation` branch without holding unrelated development
or production releases. Continuation implementation, collection, independent human
failure review, publication and a separately authorized release route remain open.
The native historical-index load measurement
under `20261013-native-postgres-load/` retains 480 successful retrieval samples through
100,000 competing records. It measures one synthetic information need and cannot
replace complete confirmatory execution, independent failure review or answer gates.
The unit tests' fabricated metric matrices are not a release
corpus, and the existing diagnostic remains ineligible. The shipped configuration
still supplies no semantic channel and all twelve gates remain pending.

The registry binds all twelve gates to explicit checks in
`tests/gates/general_memory_recall_m33.py`. These checks require the complete
reviewed publication inputs and conservatively fail together if any release
prerequisite is absent. They recompile the original collection through the same
publisher, verifying the corpus, observations, reviews, all eleven supporting
gate reports and exact committed inputs. They do not create supporting reports,
human decisions or model observations, and do not activate a channel. Supporting
report provenance remains the trusted operator's responsibility: mechanism tests,
native load measurements and comparative observations must supply their actual
recorded results rather than an invented passing receipt.

The explicit command is
`VEETBOT_M33_GATE_INPUTS=PATH uv run pytest tests/gates/general_memory_recall_m33.py`.
The operator supplies a `recall-gate-inputs@1` JSON manifest containing `identity`,
`bundle`, `independence_review`, `failure_review`, `collection_manifest` and eleven
`gate_reports` paths. Relative paths resolve beside that manifest; `identity` is
the independently captured identity. The verifier writes only a temporary
recompilation and checks that its manifest and identity remain unchanged. The file
does not match ordinary pytest discovery, so implementation CI can precede the
paid measurement; selecting a release gate without evidence fails rather than
skipping. Registry checks and their regressions are frozen before confirmation,
because test and registry changes are evaluated inputs under ADR-0199.
Both evaluation runners retain a terminal failed record, final timestamp and
settled/held costs when provider or registry setup fails after registration.
The integration clone helper force-closes leftover connections only in its own
successfully created database. Its creation epoch supports explicit operator
cleanup after a killed test; the opt-in for one scratch template does not authorize
an automatic age-based sweep of other databases or retained evaluation evidence.

## First implementation and diagnostic status (2026-10-11)

The live selector is `retrieval@5`; `memory/profiles.yaml` exposes
`retrieval.candidate_selection: relevance` with `recent` as its rollback/control
value. Both persistence adapters retain independent candidate arms. Automatic
query formation and `memory.search` select that mode explicitly. Frozen M16 and
M32 drivers explicitly select the legacy ordering, without changing their corpora,
baselines or scorer manifests.

`evals/capability/general-memory-recall-seeds.v1.tsv` holds 400 synthetic diagnostic
information needs, fifty per family. This is an initial pooled-statement diagnostic,
not the release corpus: source histories, independently reviewed lineage splits,
formation and answer/tool-use observations, and the boundary corpus remain open.
The runner never treats those unmeasured stages as passing. Reproduce the diagnostic:

```sh
uv run python -m agent_core.evals.general_memory_recall_driver --noise 1000 --output test-results/memory/general-recall-1000.json
```

At 1,000 newer distractors and the unchanged 20-item/2,000-token caps, both recall
paths increased rendered-evidence recall from 67% to 68.75%. Automatic preference
recall remained 26%; this confirms the repair does not solve implicit semantic needs.
The optional synthetic embedding comparison has verified response ordering, finite
vectors, usage, spend reservation and no retries. Initial calls using the generic
credential returned HTTP 401; the provider profile's `VEETBOT_OPENAI_KEY` worked.
On the same synthetic pool, top-20 candidate recall is 68.5% for lexical/structured
selection, 97% for dense search and 96.5% for rank fusion. The 256-dimensional
`text-embedding-3-small` experiment used 25,155 tokens at USD 0.00050310. These
candidate measurements precede token-budget rendering and answer use; they justify
continuing the semantic-index work, not activation. No production semantic index
or additional model call has been enabled.

The implementation and observations are tracked in
`evals/observations/general-memory-recall/20261011-candidate-selection/`.
The remaining gates retain their pending status; these measurements do not authorize
activation or complete Milestone 33.

The development runner also supports a guarded PostgreSQL mode. It requires explicit
scratch authorization, development settings and a database named
`veetbot_m33_eval_<name>`. A session advisory lock spans each case and prevents a
second experiment from resetting its tables. Cleanup attempts unlock even if
rollback fails and invalidates a connection whose lock cleanup fails. Ordinary cleanup,
connection-close or disposal failures cannot replace the original evaluation
failure; a cleanup failure after a successful body still fails the case. New
cancellation during cleanup always propagates.
Fixed authored histories are followed
by elapsed worker time, so leases and recall deadlines cannot remain frozen during
provider calls. Both paths execute the actual context builder and tool pipeline;
semantic experiments use the production run, reader and owner authority checks.
The PostgreSQL formation leg uses the normal, explicitly pinned `formation@11`
composition. Its configured model policy must match the measured provider policy;
the older in-memory-only People evaluation mode remains restricted. Optional
authored task-state updates pass through the existing bounded state manager and
session carry rules before submission, so task-context ablations exercise actual
working state without injecting expected evidence or answers.
Synthetic admission remains isolated and cannot activate a deployment. By default,
the deliberate path scripts one search to isolate retrieval from query planning.
The separate `search_planning: reader` ablation gives the actual reader at most one
`memory.search` call before its final answer, with no other tool or hidden retry.
Automatic recall may answer immediately when its evidence is sufficient; deliberate
recall requests the bounded search. Both use actual runtime dispatch and include
planning tokens and cost in the measurement. This is an evaluation option, not a
new unconditional production model call. Optional paired formation banks replay
identical recorded structured completions and verify identical formed current
state before each arm, as specified in [ADR-0194](../adr/0194-paired-formation-response-banks.md).
Version two banks additionally retain the narrow, completed MAX_TOKENS refusal
defined in [ADR-0215](../adr/0215-formation-terminal-outcomes.md). Recording and
replay raise the same refusal at the same request position, allowing the same
audited runtime fallback and requiring identical formed state. Legacy bank bytes
and all other unsupported or interrupted completion rejections remain unchanged.
Observations distinguish replayed formation usage from new provider work.
Reproducible sparse baseline configurations are under
`evals/capability/general-recall/experiments/`. Reviewed corpus, full comparisons,
preregistration, load experiments and release publication remain open.

## Owner-reviewed bounded release exception

[ADR-0220](../adr/0220-bounded-recall-rollout.md) records the owner's explicit
2026-10-19 authorization for a separate bounded release after the complete
1,600-observation comparison failed the original quality goals. The original
goals, failed evidence and legacy admission formats remain unchanged. This route
does not complete M33. It requires better held-out ranked/rendered recall on both
paths and no reduction in overall strict answer success against sparse control,
while retaining all numerical exceptions for actual human residual-risk review.

`recall-bounded-evidence@1` binds that complete comparison, original measured
inputs, interruptions and uncertain charges, the owner authorization, actual human
findings and all changed target files/dependencies with verification receipts.
`recall-bounded-admission@1` binds the evidence hash, independently checked target
input census, active composition dependencies, one tenant/owner, a compatible
rollback revision and a fixed lifetime of at most seven days. This is an explicit
[ADR-0218](../adr/0218-checkpoint-recall-production-compatibility.md) compatibility
assessment, never a byte-equality claim or an inherited descendant approval.

Composition permits only the local encoder and interactive private owner queries
with existing live-reader authority. Child, delegated and skill-review runs are
withheld. The existing trusted principal seed check excludes scheduled/system
messages. Source expansion still requires that live query authority. Remote
embedding composition and unattended reconsolidation refuse this route. Existing
context, search/round, latency, request and USD 2 daily indexing ceilings remain.
Expiry and evidence/source mutation latch withdrawal for the admission handle;
ordinary sparse recall remains available. Human review must cover difficult
scenarios and every interruption/uncertain-cost item, with nonempty findings.
Operator attribution and verification of the reviewer's real authorship remain
mandatory; synthetic test reviews are never release evidence.

[ADR-0222](../adr/0222-owner-approved-recall-review-scope.md) records the owner's
2026-10-20 approval of a narrower human coverage requirement for this exact retained
comparison. Version-two review, evidence and admission formats bind the immutable
approved scope: 37 individual cases, one summary of all 40 cost-exception rows,
and four summaries retaining all 18 history items. The 270 omitted difficult cases
receive no individual attestation. Each selected case and summary requires an
explicit assessment; concerns require notes, and unacceptable findings cannot
accompany rollout acceptance. A separate overall finding and residual-risk
decision remain required. The legacy full-review rule and all other safety,
compatibility and release requirements remain unchanged. The offline packet
generator preserves original evidence, starts with empty controls, saves drafts
and exports only actual scoped findings; it never creates admission evidence.

[ADR-0223](../adr/0223-owner-production-recall-trial.md) records the owner's later
request to learn from real production use. Version-three evidence carries a
separate genuine owner trial authorization, not synthetic-case findings. Its
admission expires after 24 hours unless renewed by a timely actual owner check-in
covering up to five recent eligible production runs. Each check-in binds its
selection/provenance receipt, period, owner and trial; negative decisions, missing
coverage, a serious incident or a missed deadline refuse continuation. Renewed
evidence requires re-composition and cannot move the original seven-day end.
The original comparison, compatibility and other admission requirements remain.
The [trial guide](../recall-production-trial.md) describes the conversational
review and privacy-preserving operator workflow. Neither approval of the trial
nor these mechanics establishes actual production quality.

[ADR-0244](../adr/0244-bounded-owner-recall-restart.md) represents ADR-0223's
separate restart-after-lapse decision in version-four evidence and admission.
It preserves the original trial and binds a distinct human restart instruction,
prior evidence/trial hashes, previous expiry, fresh assessment and withdrawal
receipt, exact current inputs and compatible rollback. The decision follows the
previous expiry and precedes restart; the new review window begins at restart.
Expiry is 24 hours after restart or the latest timely check-in, capped at
seven days after the original start. Prior runs and responses cannot be
reused. A recorded stop or serious incident is not cleared by this mechanism.
Only one restart phase is represented; it cannot be silently replaced after
another lapse. Operator verification of the retained receipts, final release
gates and a separate actual owner decision remain required before installation.
Preparation alone grants no activation, and legacy formats retain their rules.

## Hard gates

1. **M33.** **Corpus independence** — at least 400 independent scenarios span eight families,
   with disjoint development/holdout lineage; variants do not inflate the count.
2. **M33.** **Relevance before limits** — older stronger matches survive newer weak matches
   in both adapters, including historical selection and independent arm budgets.
3. **M33.** **Visibility parity** — every channel preserves principal, lifecycle, sensitivity,
   portability, explicit exclusions, corrections and erasure before result limits.
4. **M33.** **Shared recall** — automatic and deliberate retrieval use the same search
   semantics and retain known-incident regressions without snapshot promotion.
5. **M33.** **Query planning bounds** — assisted search is causally blind to labels, bounded,
   cache-safe and cancellation-safe, with no new uncached fast-path model call.
6. **M33.** **Semantic index lifecycle** — derived entries bind source/model revisions;
   correction, restriction, deletion and stale backfills cannot expose old content.
7. **M33.** **Grounded recovery** — bounded source expansion preserves provenance, trust and
   deletion/correction fences without re-extraction or unauthorized history access.
8. **M33.** **Stage attribution** — observations distinguish formation, candidate, rank,
   context-budget and evidence-use failures, with deterministic mechanical scoring.
9. **M33.** **Comparative quality** — all incident, recall, implicit-use, category-regression
   and hard-boundary thresholds pass on the frozen held-out inputs and budgets.
10. **M33.** **Operational budget** — numerical preregistered cost/latency ceilings, finite
    retries, shared deadlines, partial failure and cancellation are enforced.
11. **M33.** **Frozen compatibility** — old control artifacts remain byte-identical and
    reproduce under their explicit legacy policy; new evidence is independently versioned.
12. **M33.** **Activation evidence** — only matching complete passing evidence admits optional
    channels; rollback preserves originals and never enables unattended dreaming.
