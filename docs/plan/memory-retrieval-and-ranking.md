---
title: Memory Retrieval & Ranking
status: design
canonical: true
---

# Memory retrieval and ranking

This document specifies the **read path** for long-term memory: how a goal becomes
a query, how candidate beliefs are generated and ranked, and how a small, safe,
budget-fitting set is paged back into working memory. It is the companion to
[memory formation and consolidation](memory-formation-and-consolidation.md), sits
under Milestone 9 of the [engineering plan](engineering-plan.md), and is recorded
as [ADR-0019](../adr/0019-memory-retrieval-and-ranking.md).

Scope: **retrieval and ranking** of beliefs and episodes. Formation is specified
separately; the entity-graph layer is a later spec and appears here only as a
recall arm that is not yet built.

## Why retrieval is its own problem

Formation decides what is true. Retrieval decides what is *relevant right now*,
and it operates under three constraints formation does not have:

- **The context window is scarce and priced.** Every recalled belief displaces
  history or tool results and is paid for on every request of the session.
- **The prompt prefix is cache-sensitive.** Naive "inject the latest memories"
  designs invalidate the provider prompt cache on every turn, which is both the
  largest cost regression available and a latency regression (Section 10.1).
- **Recall is on the critical path of the turn**, where formation is not.

The failure mode is therefore not "missed a fact" but **dilution**: a large,
loosely-relevant memory block measurably degrades answers while costing more than
it returns. This spec optimizes for **a small, current, high-signal set** and
treats every retrieved item as something that must earn its tokens.

## What retrieval must respect

These are fixed by earlier decisions and are not re-litigated here.

- **Prompt-stability invariant (Section 10.1, ADR-0012).** The cacheable prefix is
  built once per session and stays byte-stable. Memory is injected as a **frozen
  per-session snapshot** in that prefix; every mid-session recall goes into the
  **user turn**, never into the prefix (ADR-0014).
- **Memory is data, never instructions.** Recalled items carry `TrustLevel.MEMORY`
  (Section 11.2) and can never redefine policy, grant permission, or change
  approval requirements. Memory is the one *stored and replayed* injection vector,
  so it is scanned at load and poisoned entries are replaced with `[BLOCKED]`
  placeholders (ADR-0014).
- **Scope is a hard filter.** Tenant, principal, and scope are query predicates,
  never ranking features. There is no score high enough to cross a tenant.
- **Confidence and lifecycle state matter (formation spec, "Memory states and
  tiers").** `active` beliefs retrieve at full weight; `provisional` retrieves
  weakly; `superseded` and `expired` are excluded from live recall.
- **Every recall is traced.** "Memory retrieval traces" is a Milestone 9
  requirement, and the trace is what makes ranking tunable.
- **Postgres full-text search first.** `pgvector` is added only when evaluations
  show a material benefit (Milestone 9).

## Three retrieval moments

The cache invariant forces retrieval to happen at distinct moments with different
budgets, profiles, and destinations. This is the central shape of the design.

**1. Session-open snapshot (the core).** Built once when a session starts, from a
structured query rather than a text query: the user-model beliefs and high-durability
preferences that are relevant to almost any turn, plus a small priming set derived
from the session's opening goal. It is rendered into the cacheable prefix and never
changes for the life of the session. Profile: `core`. This is the "who you are
talking to" layer, and it is the only memory the model gets for free.

**2. In-turn recall (the task layer).** Mid-session, when the turn needs something
the snapshot does not carry. Two triggers: an automatic pre-turn recall when the
query former's confidence in a relevant hit clears a floor, and the agent's
explicit `memory.search` tool for deliberate lookup. Results are injected into the
**user turn** adjacent to the goal, so the prefix stays byte-stable. Profile: `task`.

**3. Child-run recall.** Subagents and consolidation runs (Section 27.6) get their
own scoped recall against their own budget — never a copy of the parent's snapshot.
A child researching an unrelated subtask should not inherit the parent's user model
wholesale, and a consolidation run needs a *belief-neighbourhood* query, not a
conversational one.

### The recall delta

Because the snapshot is frozen, beliefs formed or corrected mid-session are invisible
to it. The session carries a `snapshot_watermark` (the belief-store position at
snapshot time). Any belief created, reinforced, or superseded after that watermark is
eligible for in-turn injection, and a correction to a belief *inside* the snapshot is
injected as an explicit override in the user turn:

```text
correction: [m:8f21] no longer holds as of 2026-07-24; superseded by [m:9d02].
```

The prefix is never rewritten. The stable platform text tells the model that
corrections and the current user turn outrank the memory block.

## Beliefs and episodes are different queries

The memory hierarchy has two retrievable stores below working memory, and conflating
them is a common design error.

| Question | Store | Retrieval shape |
| --- | --- | --- |
| "What is true?" | belief store (semantic) | ranked, deduped, current-as-of |
| "What happened?" | event log (episodic) | scoped by session/time, ordered, not deduped |
| "What happened long ago?" | archival episodes | explicit, budgeted, agent-invoked only |

Beliefs are the default. Episodic search is an **escalation**: it runs when the query
is explicitly about events ("what did we decide on Tuesday"), or when the belief store
returns nothing above the floor for a subject the user is clearly referring to.
Archival recall is never automatic — it is an agent-invoked tool with its own budget,
because it is the expensive tier.

Formation promotes episodic to semantic; retrieval pages both back into working
memory. Working memory itself (Section 7 `WorkingState`) is not retrieved — it is
already in context, and it is the *destination*.

## The pipeline

Given a goal and the current working state:

```text
1 form query    ─▶  2 hard filter   ─▶  3 recall arms
                                             │
6 collapse      ◀─  5 rank          ◀─  4 fuse
     │
     ▼
7 safety        ─▶  8 assemble      ─▶  9 trace & feed back
```

### 1. Form the query

The most common retrieval bug is embedding the raw last user message. "Can you redo
that?" carries no retrievable signal; the *task* does. The query former builds one or
more `RecallQuery` objects from:

- the working-state **objective** and open questions (Section 7), which persist across
  turns and describe the actual task;
- **active entities** — subjects mentioned in the current turn or recently, expanded
  through a subject-alias table ("Acme" / "Acme Corp");
- the **current message**, as one signal among several rather than the whole query;
- the **scope** implied by the session (project, agent, principal).

The former emits a small set of queries, not one: a structured query for the entities
in play, and a text query for the intent. Query formation is a replaceable strategy
(`QueryFormer`) and starts deterministic — no model call on the critical path. A model-
assisted former is a later optimization, gated by evals, and never on the fast path
without a cache.

### 2. Hard filter

Applied in the SQL predicate, before any scoring:

- `tenant_id`, `principal_id`, and permitted `scopes`;
- lifecycle: exclude `superseded` and `expired` unless the query is historical;
- bi-temporal validity: `valid_from <= as_of AND (valid_to IS NULL OR valid_to > as_of)`,
  where `as_of` defaults to now. This is what makes "what did I believe last month" a
  first-class query rather than an archaeology exercise;
- sensitivity ceiling for the current surface (a memory that is fine in a private
  session may not be renderable to a shared or inbound surface — Section 22).

A scope filter implemented as a rank feature is a defect, not a tuning choice.

### 3. Recall arms

Arms run **concurrently** with a shared deadline; a slow arm degrades to partial
results rather than delaying the turn.

- **Structured** (always on). Direct lookup by subject, `belief_type`, and scope. No
  scoring needed. This is what builds the snapshot core and what answers "what do you
  know about X" precisely.
- **Lexical** (always on). Postgres full-text search over `subject` + `statement` with
  `ts_rank_cd`, plus trigram similarity for names and typos. Cheap, debuggable, and
  strong on the proper nouns that dominate memory queries.
- **Semantic** (optional, off at first). `pgvector` over statement embeddings. This is
  where paraphrased preferences are found ("keep it brief" vs "prefers concise
  writing") — the case lexical genuinely misses. It is built behind the fusion
  interface as a config flip and enabled only when the eval harness shows lift, per
  Milestone 9.
- **Graph expansion** (later). One-hop expansion over entity relationships, handed to
  the entity-graph spec. Until then, alias expansion in the query former covers the
  cheap part of the same need.
- **External provider** (later). An external memory provider (ADR-0014) is an
  additional arm behind the same fusion, not a replacement for the pipeline.

### 4. Fuse

Combine arms with **reciprocal rank fusion**: `score_fused(b) = Σ_arms 1 / (k + rank_arm(b))`
with `k = 60`. RRF is chosen because it needs no score calibration across arms —
`ts_rank_cd` and cosine similarity are not comparable quantities, and every attempt to
normalize them into a shared scale is a source of silent drift. Adding or removing an
arm does not require retuning the others.

### 5. Rank

The fused match score is one feature among several. The ranking function is explicit,
hand-weighted, and versioned:

```text
score(b, q) =
      w_match     · match(b, q)           # fused arm score, 0..1
    + w_conf      · conf(b)               # confidence x lifecycle state
    + w_reinforce · reinforce(b)          # corroboration, time-decayed
    + w_authority · authority(b)          # user > affirmed > inferred
    + w_scope     · scope_affinity(b, q)  # project > user > global
    + w_utility   · utility(b)            # usefulness when recalled
    - w_penalty   · penalty(b)            # stale, flagged, near-duplicate
```

with:

- `conf(b) = confidence × {active: 1.0, provisional: 0.4}` — provisional beliefs are
  retrievable but never dominant, and never enter the snapshot core;
- `reinforce(b) = log1p(corroboration_count) / log1p(C_max) × exp(-Δt / τ)`, where
  `Δt` is time since `last_reinforced_at` and `τ` varies by `belief_type` — stable
  preferences decay slowly, situational facts quickly;
- `authority(b)` from the belief's provenance: direct user statement, agent-affirmed
  conclusion, or inference;
- `penalty(b)` covers `flagged_for_review`, being past `expires_hint`, and being a
  near-duplicate of a higher-ranked item.

Two weight vectors, one function. The `core` profile (snapshot) drops `w_match` to
zero — there is no query — and raises durability and authority. The `task` profile
weights match and scope affinity heavily. Weights are hand-set, recorded as
`retrieval_policy_version` on every trace, and tuned against the eval harness. A
learned reranker is deferred until traces provide labeled data; hand weights that can
be explained beat an unexplainable model at this stage.

Below a **relevance floor**, candidates are dropped even if the budget is unspent.
Filling the budget is not the objective.

### 6. Collapse and diversify

- **Supersession collapse.** Group by `(subject, belief_type, predicate key)` and keep
  the belief valid at `as_of`. Never surface a belief alongside the one it replaced.
- **Unresolved conflicts surface as conflicts.** Where two beliefs are linked by
  `conflicts_with` with no supersession, return **both** with an explicit marker rather
  than silently picking one. Formation deliberately refused to resolve these; retrieval
  must not quietly resolve them at read time.
- **Per-subject cap.** At most *n* beliefs per subject, so one heavily-discussed entity
  cannot consume the budget.
- **Redundancy trim.** Drop near-duplicates by statement similarity, keeping the
  higher-scored one and merging their provenance in the trace.

### 7. Safety pass

- **Injection scan** each candidate statement at load; failures render as `[BLOCKED]`
  placeholders with their ids preserved so the user can inspect and correct the
  poisoned entry (ADR-0014). Blocking is per-item; one bad belief never suppresses the
  whole block.
- **Sensitivity redaction** against the current surface's ceiling.
- **Never render** secrets, credentials, or private reasoning — formation should have
  prevented these from existing, and retrieval is the second gate.

### 8. Assemble to budget and render

Fit the ranked set into `ContextBudget.retrieved_context_tokens` (Section 11.3),
highest score first, stopping at the budget or the floor. Then render a
**deterministic, trust-labeled block**:

```text
<memory as_of="2026-07-24T09:00:00Z" policy="retrieval@3">
  [m:8f21] Andy prefers concise, prose-first writing.   (user-stated, high)
  [m:1c07] Andy's current project is veetbot.           (user-stated, high)
  [m:44b9] Andy's stack is Postgres-backed.             (inferred, medium)
  [m:0d3e] [BLOCKED] withheld: failed injection scan.
</memory>
```

Rendering rules exist to protect the cache and the trust boundary:

- **Total order** by `(score desc, belief_id asc)` — never insertion order, which is
  nondeterministic across concurrent arms.
- **Confidence bands, not floats.** `high` / `medium` / `low`. Raw scores would make
  the block churn on trivial drift.
- **No volatile fields.** No wall clock, no "retrieved 3 minutes ago", no per-request
  counters. The only timestamp is the fixed `as_of`. A snapshot for the same belief set
  must render byte-identically or the cache breaks.
- **Framing lives in the stable prefix, not in the block.** The instruction that these
  are recollections which may be stale, that the current turn outranks them, and that
  the model should cite an id when it relies on one, is platform text in the cached
  prefix. Only the data varies.
- **Ids are exposed** so the model can cite, the trace can be joined, and the user can
  say "that one is wrong" about a specific belief.

### 9. Trace and feed back

Every recall emits a `RecallTrace` and a `memory.recalled` event: the query, the arms
and their latencies, candidate counts, what was returned, what was dropped for budget,
what was blocked, and the `retrieval_policy_version`. Traces are the tuning data.

Retrieval then feeds back into formation, in both directions:

- **Used beliefs resist decay.** When a belief is cited or demonstrably used, it
  extends `last_reinforced_at` for decay purposes and raises `utility`. It does
  **not** raise `confidence` — see the decision below.
- **Retrieved-but-never-used** lowers `utility`, so a belief that keeps winning the
  ranking without ever mattering stops winning it.
- **Recall misses are a formation signal.** A subject that is repeatedly queried and
  repeatedly returns nothing above the floor is queued as a targeted re-derivation
  hint for the formation loop (formation stage 8). The read path tells the write path
  what it should have remembered.

## Ports and data model

```python
class RecallQuery(BaseModel):
    tenant_id: str
    principal_id: str
    scopes: list[str]                  # hard filter: user / project / global
    text: str | None = None            # intent; may be multi-sentence
    subjects: list[str] = []           # structured anchors, alias-expanded
    belief_types: list[str] = []
    as_of: datetime | None = None      # bi-temporal; None means now
    include_superseded: bool = False   # historical queries only
    profile: str = "task"              # "core" | "task" | "deep"
    budget_tokens: int
    max_items: int
    min_score: float                   # relevance floor

class RecalledBelief(BaseModel):
    belief_id: UUID
    subject: str
    statement: str
    belief_type: str
    status: str                        # active | provisional | ...
    confidence_band: str               # "high" | "medium" | "low"
    authority: str                     # "user" | "affirmed" | "inferred"
    valid_from: datetime
    valid_to: datetime | None
    score: float
    arms: list[str]                    # which arms produced it
    conflict_with: list[UUID] = []     # unresolved conflicts, not hidden
    blocked: bool = False

class RecallResult(BaseModel):
    items: list[RecalledBelief]
    rendered: str                      # trust-labeled block, byte-stable
    tokens: int
    truncated: bool
    arms_degraded: list[str]           # arms that timed out
    trace_id: UUID

class RecallTrace(BaseModel):
    id: UUID
    session_id: UUID
    run_id: UUID | None
    query: RecallQuery
    arm_latencies_ms: dict[str, int]
    candidates: int
    returned: list[UUID]
    dropped_for_budget: list[UUID]
    blocked: list[UUID]
    retrieval_policy_version: str
    created_at: datetime
```

Ports, all replaceable strategies behind the existing memory port (ADR-0014):

```python
class MemoryRetriever(Protocol):
    async def recall(self, query: RecallQuery) -> RecallResult: ...

class QueryFormer(Protocol):
    def form(
        self,
        run: Run,
        working_state: WorkingState,
        message: str | None,
    ) -> list[RecallQuery]: ...

class Ranker(Protocol):
    def rank(
        self,
        candidates: list[RecalledBelief],
        query: RecallQuery,
    ) -> list[RecalledBelief]: ...

class EpisodeSearch(Protocol):
    async def search(self, query: EpisodeQuery) -> list[EventEnvelope]: ...
```

## The agent-facing surface

Two tools, both returning `TrustLevel.MEMORY` data:

- **`memory.search`** — deliberate belief lookup: text, optional subject and
  `belief_type` filters, optional `as_of`. Returns ranked beliefs with ids. Subject to
  the same hard filter, safety pass, and budget as automatic recall.
- **`memory.recall_episodes`** — the escalation path into episodic and archival
  history, scoped by time or session, with its own budget. Not automatic.

Both are ordinary tools: policy-gated, traced, and counted against the run budget.
Neither can return anything the hard filter excludes.

## Failure modes and defenses

| Failure | Defense |
| --- | --- |
| Context dilution from over-retrieval | hard budget, relevance floor, per-subject cap; measured as noise ratio |
| Stale belief surfaces as current | `as_of` filtering, supersession collapse, time-decayed reinforcement |
| Poisoned memory replayed every session | load-time injection scan to `[BLOCKED]`; memory is data; policy is unreachable from memory |
| Prompt cache invalidated by memory | frozen byte-stable snapshot; all mid-session recall into the user turn |
| Cross-tenant or cross-scope leak | scope is a SQL predicate, never a feature; asserted per query path in tests |
| High-value item buried mid-block | small blocks, highest score first, task recall adjacent to the goal |
| Retrieval latency on the critical path | concurrent arms with a shared deadline, partial-result degradation, precomputed snapshot |
| Wrong belief entrenched by being useful | usage resets decay but never raises confidence |

## Evaluation

Retrieval evaluation shares the harness with formation (Section 20), and building it
is what finally makes formation measurable end to end — formation quality cannot be
observed until beliefs can be read back.

- **Consequential recall@k** — of the facts a later task needs, how many are retrieved
  within budget.
- **Noise ratio** — retrieved-and-irrelevant over retrieved. The counterweight to
  recall; both are reported, never recall alone.
- **Currency** — after a preference change, retrieval must return the new belief and
  never the superseded one.
- **Historical correctness** — an `as_of` query returns what was believed then.
- **Injection resistance** — a poisoned belief renders `[BLOCKED]` and does not alter
  behavior.
- **Scope isolation** — zero cross-tenant and cross-principal results. A hard gate,
  not a metric to improve.
- **Cache preservation** — measured cached-token ratio must not regress when memory is
  enabled. This is the invariant's regression test.
- **Cost and latency** — retrieval tokens per turn and p95 recall latency within budget.
- **End-to-end lift** — LOCOMO-style multi-session scenarios, the metric that justifies
  the whole layer.

Gate: retrieval improves target eval cases **without** increasing policy failures,
without regressing cache utilization, and without raising noise ratio.

## Build sequence (incremental, each gated by evals)

1. **Deterministic core.** Structured scope-filtered lookup, session-open snapshot,
   byte-stable rendering, trust labeling, and traces. No ranking beyond confidence and
   recency. This alone gives the agent a stable user model.
2. **Lexical recall and ranking.** Postgres FTS arm, the hand-weighted ranker, the
   relevance floor, budgeted assembly, supersession collapse.
3. **In-turn recall.** `memory.search`, automatic pre-turn recall with a floor, and the
   recall delta over the frozen snapshot.
4. **Feedback loop.** Usage tracking into decay resistance and `utility`; recall misses
   into formation re-derivation hints.
5. **Semantic arm.** `pgvector` plus RRF fusion — only if the harness shows lift over
   lexical (Milestone 9).
6. **Later arms.** Entity-graph expansion (graph spec), external provider arm
   (ADR-0014), learned reranker once traces provide labeled data.

## Decisions

- **Retrieval happens at distinct moments with distinct profiles**: a frozen
  session-open snapshot in the cached prefix, in-turn recall into the user turn, and
  separately-scoped child-run recall. The prefix is never rewritten mid-session.
- **Scope is a filter, never a ranking feature.** No score crosses a tenant.
- **Superseded and expired beliefs are excluded from live recall**, but genuinely
  unresolved conflicts are surfaced as conflicts rather than silently resolved at read
  time — retrieval does not undo formation's refusal to guess.
- **Provisional beliefs are retrievable but down-weighted** and never enter the
  snapshot core.
- **Usage resets decay but never raises confidence.** Otherwise a wrong belief that
  happens to rank well entrenches itself by being retrieved — evidence must come from
  the world, not from the retriever.
- **Fusion is reciprocal-rank, not score-normalized**, so arms can be added or removed
  without recalibration.
- **Ranking weights are hand-set, versioned, and eval-tuned**; a learned reranker waits
  for labeled trace data.
- **Lexical plus structured first**; the semantic arm ships behind the fusion interface
  and is enabled on eval evidence, per Milestone 9.
- **Every recall is traced**, and traces are the feedback channel into both ranking and
  formation.

## Open questions

1. **Snapshot budget.** How large should the always-on session-open core be — a hard
   token cap (proposed: roughly 1–2% of the context window) or a belief count? It is
   paid on every request of the session, so this is a direct cost-versus-continuity
   trade.
2. **Cross-project bleed.** Should a highly relevant project-scoped belief ever surface
   in a different project? Proposed default: no — user and global scope only, with an
   explicit per-agent opt-in — but that costs some genuinely useful recall.
3. **User-visible retrieval traces.** Should "why did you say that" expose the beliefs
   used in an answer to the end user, or should traces stay an operator surface? The
   transparency argument is strong; it is a product surface with its own cost.
