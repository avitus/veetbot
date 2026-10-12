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

Intent and inferred dates can boost matches but cannot become mandatory filters
without an explicit user constraint and a tested fallback. Failure to find a
memory is not proof that the underlying fact is false. A second search targets
missing evidence; it cannot invent a value to make an answer complete.

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

## Local semantic index implementation

[ADR-0179](../adr/0179-versioned-semantic-index.md) implements the derived-index
boundary after the measured pilot. `SemanticMemoryIndex` captures version-bound
originals, compare-and-swaps computed vectors, and supplies bounded immutable-order
inventory and change pages. It does not serialize sources to a provider or schedule
backfill. `memory_vectors` uses pgvector exact cosine search, owner-bound foreign
keys and forced tenant RLS. Both adapters remove vectors transactionally on source
changes and rejoin current permitted originals before the result limit. Changes to
usage alone do not re-embed content. Historical and People-scoped queries currently
retain only their existing sparse path; they never reuse current vectors as past
or identity-certified evidence.

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

Production supplies no lookup and `semantic_enabled` remains false. A populated
index alone cannot activate a provider or change production recall. The bounded
indexing service now implements explicit provider admission, durable reservations
and resumable inventory under [ADR-0180](../adr/0180-bounded-semantic-indexing.md).
It is not scheduled in production. Explicit bounded query preparation now populates
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
Production admission, the authority resolver, explicit preparation scheduling and
indexing maintenance scheduling remain open; release evidence must cover them and
the withheld query modes.
Frozen M16/M32 controls continue to select legacy candidates without the semantic
arm. The complete M33 gates remain pending.

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

Every scenario exercises automatic and deliberate recall. Record both correctly
seeded retrieval and the full formation-to-answer path. Include old relevant facts,
newer unrelated facts, hard semantic distractors, another person's facts, conflicts,
negation, updates, deletion, historical questions and information never supplied.
Load tests use 100, 1,000 and 10,000 competing records, with a separately measured
100,000-record PostgreSQL experiment before production scaling claims.

Score stages separately: formed, candidate selected, ranked, rendered and used.
Use deterministic identifiers and fixed clocks; count exact evidence IDs before
rendering and mechanically labeled values/constraints in answers or proposed tool
arguments. Tool-use evaluations do not perform real external actions. Report
retrieved-but-unused facts, hallucinated defaults, stale use and false transfer.
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
