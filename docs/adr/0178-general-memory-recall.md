# ADR-0178: General memory search and recall

- Status: Accepted scope; implementation and activation require the gates below
- Date: 2026-10-11
- Authorization: the owner approved the six-stage, literature-based recall plan
  with "I authorize the plan. Continue".
- Related: ADR-0019, ADR-0069, ADR-0077, ADR-0169, ADR-0174
- Detailed design: `docs/plan/general-memory-recall.md`

## Context

An older stored personal fact was unavailable to an advice question. A narrow
ownership lookup recovered that incident, but a non-ownership preference failed
the same way. Any-term matching followed by newest-first candidate truncation
loses relevant records before ranking. Literal query formation also misses the
unstated information required by a task. Neither a larger recent window nor
another category-specific regular expression addresses both causes.

The owner authorized the research plan, including controlled semantic retrieval
and bounded source recovery from roadmap B6. Authorization does not establish
quality, approve production deployment, or enable unattended reconsolidation.

## Decisions

1. Admit general recall as parallel Milestone 33, without moving the sequential
   verified ceiling. Register twelve gates before runtime implementation.
2. Replace ADR-0069 decision 6's newest-first candidate ordering with relevance
   before truncation, independent structured/lexical candidate budgets and
   deterministic merging. Keep explicit legacy selection for frozen controls.
   Literal browsing and snapshot/delta chronology remain separate operations.
3. Evaluate at least 400 independent scenarios across eight families. Keep
   paraphrases and distractor variants distinct from the independent-case count;
   split by scenario and query template. Separate formation, candidate recall,
   ranked recall, context selection and answer/tool-argument use.
4. Compare lexical/structured, semantic-only and fused retrieval at matched
   context budgets. Add pgvector only after semantic evidence demonstrates
   material benefit. Embeddings and search metadata are derived indexes, never
   user assertions or evidence. Source correction, restriction and erasure
   invalidate all dependent entries and caches before reuse.
5. Form bounded searches from task context and unresolved information needs.
   Preserve the deterministic fast path; model-assisted formation is cached or
   an explicitly bounded slower escalation. Inferred intent/time labels are
   relevance signals, never authority to cross or narrow hard visibility rules.
6. Automatic and deliberate recall share candidate generation, ranking, trust
   handling and evidence packaging. Supporting-source recovery is bounded and
   governed; no deleted, rejected, unavailable or superseded assertion may be
   revived from source text. Archival recovery remains agent-invoked.
7. Keep old M16/M32 corpora, baselines, scorer hashes, observations and decisions
   unchanged. New policy evidence is separate and names source revision, corpus,
   model/index versions, budgets and all failed observations. A new policy does
   not make historical certificates current or waive M32 activation rules.
8. Ship the category-independent candidate repair with its regressions. Enable
   semantic search, assisted queries, reranking and source recovery only on
   their measured contribution and complete privacy/cost contracts. Remove the
   ownership heuristic only after the general path passes those regressions.
9. No external memory service, arbitrary graph inference, training/RL, model
   routing, automatic historical re-extraction or production memory rewrite is
   authorized by this change. Existing provider residency and experiment
   consent rules apply. Synthetic evaluations do not authorize exporting real
   owner conversations.

## Evidence and alternatives

The detailed design records the research, experiments, numerical gates and
implementation order. A keyword fix alone cannot handle implicit intent;
semantic similarity alone does not establish correct person, time, applicability
or use. Every proposed additional mechanism must beat a strong lexical control
at the same context budget. The new work does not claim the target metrics pass.
