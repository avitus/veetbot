# ADR-0019: Memory retrieval and ranking

- Status: Proposed
- Date: 2026-07-24
- Related: Milestone 9 (long-term memory and knowledge retrieval), ADR-0018 (memory
  formation and consolidation), ADR-0014 (memory surface, frozen snapshot, injection
  scanning), ADR-0012 (prompt-stability invariant), Section 11 (context engine),
  Section 20 (evaluation), Section 27.6 (child runs)
- Detailed design: `docs/plan/memory-retrieval-and-ranking.md`

## Context

ADR-0018 specifies the write path. The read path has its own constraints: context is
scarce and priced per request, the prompt prefix is cache-sensitive, and recall sits on
the critical path of the turn. The dominant failure mode is not a missed fact but
**dilution** — a large, loosely-relevant memory block degrades answers while costing
more than it returns. Retrieval is also where a poisoned belief would be replayed, so
it is the enforcement point for the memory trust boundary as well as a ranking problem.

Retrieval is additionally the prerequisite for measuring formation: belief quality
cannot be observed end to end until beliefs can be read back into a task.

## Decision

1. **Retrieval happens at three distinct moments with distinct profiles**: a
   session-open **snapshot** (structured, frozen, rendered into the cached prefix), an
   **in-turn recall** (query-driven, injected into the user turn), and separately-scoped
   **child-run recall**. The cached prefix is never rewritten mid-session; a
   `snapshot_watermark` plus explicit in-turn corrections handle beliefs that change
   after the snapshot is taken.
2. **Beliefs and episodes are different queries.** The belief store answers "what is
   true" (ranked, deduped, current-as-of); the event log answers "what happened"
   (scoped and ordered). Episodic search is an escalation, and archival recall is
   agent-invoked only.
3. **Query formation is deterministic and derives from the working state**, not from
   the raw last user message; it emits a small set of structured and text queries. A
   model-assisted former is a later, eval-gated optimization and never uncached on the
   fast path.
4. **Scope is a hard filter, never a ranking feature.** Tenant, principal, scope,
   lifecycle state, bi-temporal validity at `as_of`, and the surface sensitivity ceiling
   are SQL predicates applied before scoring.
5. **Recall is multi-arm and fused by reciprocal rank** (`k = 60`): structured and
   lexical (Postgres FTS plus trigram) always on; semantic (`pgvector`), graph
   expansion, and an external provider are additional arms behind the same fusion. RRF
   is chosen so arms need no cross-calibration and can be added or removed
   independently.
6. **Ranking is an explicit hand-weighted function** over match, confidence × state,
   time-decayed corroboration, source authority, scope affinity, historical utility, and
   penalties — with two weight vectors (`core`, `task`) over one function, versioned as
   `retrieval_policy_version`. A learned reranker waits for labeled trace data.
7. **Superseded and expired beliefs are excluded from live recall**, but genuinely
   unresolved conflicts are **surfaced as conflicts** rather than silently resolved at
   read time. Provisional beliefs are retrievable, down-weighted, and never enter the
   snapshot core.
8. **Rendering is deterministic and byte-stable**: total order by `(score, id)`,
   confidence bands rather than floats, no volatile fields, ids exposed for citation and
   correction, and the framing instruction held in the stable prefix rather than in the
   data block.
9. **A relevance floor governs, not the budget.** Candidates below the floor are dropped
   even when budget remains; filling the context is not the objective.
10. **Retrieval enforces the memory trust boundary**: items are `TrustLevel.MEMORY` data,
    injection-scanned at load with per-item `[BLOCKED]` placeholders, sensitivity-redacted
    per surface, and unable to affect policy or approvals.
11. **Usage feedback resets decay but never raises confidence.** Citation extends decay
    resistance and `utility`; only evidence from the world changes confidence. This
    prevents a wrong belief from entrenching itself by ranking well.
12. **Recall misses are a formation signal**: repeatedly querying a subject and returning
    nothing above the floor queues a targeted re-derivation hint for the formation loop.
13. **Every recall emits a `RecallTrace`**, which is both the Milestone 9 retrieval-trace
    requirement and the tuning and feedback channel.

## Consequences

- Memory improves answers without invalidating the prompt cache — the cached-token ratio
  becomes an explicit regression test rather than an unmeasured casualty.
- The agent gets a stable "who am I talking to" layer from step 1 of the build sequence,
  before any ranking machinery exists.
- Formation becomes measurable end to end, unblocking the ADR-0018 eval gates.
- Added machinery: `MemoryRetriever`, `QueryFormer`, `Ranker`, and `EpisodeSearch` ports,
  `RecallQuery` / `RecalledBelief` / `RecallResult` / `RecallTrace` models, two agent
  tools, and a trace store whose volume is non-trivial.
- Hand-set weights need periodic tuning against the harness, and the fused multi-arm
  design means retrieval quality depends on eval discipline rather than on a single
  tunable knob.

## Alternatives considered

- **Inject the top-k most recent memories every turn**: rejected; invalidates the prompt
  cache on every request, dilutes context, and has no defense against stale beliefs.
- **Embed everything and rank by cosine similarity alone**: rejected; ignores confidence,
  authority, recency, and supersession — precisely the fields formation exists to
  maintain — and performs worst on the proper nouns that dominate memory queries.
- **Score-normalized fusion across arms**: rejected in favor of RRF; `ts_rank_cd` and
  cosine similarity are not comparable quantities, and normalization is a standing source
  of silent drift.
- **Model-driven query formation on every turn**: rejected as the default; a model call on
  the critical path costs latency and tokens before the turn's real work begins.
- **Tool-only recall (no automatic retrieval)**: rejected as the sole path; it makes
  continuity depend on the agent remembering to ask, which is the failure this layer
  exists to prevent. Retained as the deliberate-lookup half of the design.
- **Learned reranker from the start**: rejected; there is no labeled data yet, and an
  unexplainable ranker cannot be debugged against the eval harness.
