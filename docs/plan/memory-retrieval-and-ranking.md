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
  **user turn**, never into the prefix (ADR-0014). The prefix itself — its region
  membership rule, its epoch semantics, and the test that enforces its stability —
  is specified in [the context engine](context-engine.md) and ADR-0020. The snapshot
  is a Region A item; in-turn recall and correction lines are Region B items.
- **Memory is data, never instructions.** Recalled items carry `TrustLevel.MEMORY`
  (Section 11.2) and can never redefine policy, grant permission, or change
  approval requirements. Memory is the one *stored and replayed* injection vector,
  so it is scanned at load and poisoned entries are replaced with `[BLOCKED]`
  placeholders (ADR-0014).
- **Isolation boundaries are hard filters. Relevance boundaries are not.** Tenant,
  principal, and the surface sensitivity ceiling are query predicates with no score
  high enough to cross them. **Project is a relevance boundary, not an isolation
  boundary** — see "Beliefs carry across projects".
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
correction: [m:8f21] no longer holds as of 2026-07-24;
            superseded by [m:9d02].
```

The prefix is never rewritten. The stable platform text tells the model that
corrections and the current user turn outrank the memory block.

## Sizing the snapshot

The binding constraint on the snapshot is not token cost. It sits in the cached prefix
and is read at cache rates, so carrying it is cheap. The constraint is **attention**.
The snapshot is the one retrieval channel with no query behind it, so its precision is
structurally lower than in-turn recall's, and every belief in it is read on turns where
it is irrelevant. Dilution tracks the **absolute number** of irrelevant items, not the
fraction of the window they occupy, so the cap is absolute tokens with a percentage
ceiling rather than a pure percentage. A larger context window is not a reason for a
larger snapshot.

Pulling the other way: the snapshot is the safety net for an imperfect query former.
Anything the former fails to fire on is simply lost unless the snapshot carries it.
Correct snapshot size is therefore **inversely proportional to retrieval quality** —
larger while the former and ranker are young, shrinking as they improve. The numbers
below are a starting point with a shrink path, not a constant.

Two caps apply and whichever binds first wins. The **item cap is primary**, because
dilution counts items; the token cap is the backstop against unusually verbose beliefs.

| Session type | Items | Tokens | Ceiling |
| --- | --- | --- | --- |
| Interactive | 40 | 1,500 | 2% of window |
| Async / long-running run | 80 | 3,000 | 2% of window |
| Child run | 15 | 500 | 2% of window |

At roughly 25 tokens per rendered belief, 1,500 tokens is about 40 beliefs — close to
what a well-briefed colleague holds about you before a conversation starts: who you
are, what you are working on, how you like to work, and a handful of standing
constraints. Past that you are into facts that are relevant *sometimes*, which is what
the task layer is for. An async run gets double because it amortizes one fixed block
over many requests and knows its objective up front, where a short interactive session
pays a much larger share of its total tokens for the same block. Child runs get a small
scoped budget rather than a copy of the parent's snapshot.

Within the cap, roughly two-thirds of the item budget is reserved for durable
user-model and preference beliefs and the remainder for the opening-goal priming set,
so a burst of project-specific beliefs cannot evict "who am I talking to" — the one
thing the snapshot exists to carry. Unused priming slots are not backfilled. The
relevance floor still governs: the cap is a ceiling, not a target.

The number is then set empirically, against two signals already present in the trace:

- **Snapshot utilization** — the fraction of snapshot beliefs cited at least once in
  the session. Sustained utilization below about a quarter means the core is carrying
  passengers, and it should shrink.
- **Snapshot misses** — in-turn recalls returning a belief that was snapshot-eligible
  but did not make the cut. Frequent misses mean the core is too small, or that the
  wrong things are in it.

The two pull in opposite directions, which is the point: tune until neither is firing.
Both derive from `RecallTrace`, so retuning is a query rather than an experiment.

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

## Beliefs carry across projects

The agent learns from every project and environment it works in, and what it learns
in one is available in all of them. A lesson earned once should not have to be
taught again in the next repository.

This requires separating two things that the word "scope" conflates:

- **Isolation boundaries** — tenant, principal, and the surface sensitivity ceiling.
  These are security boundaries. They are SQL predicates applied before scoring, and
  no relevance score can cross them. A scope filter implemented as a rank feature is
  a defect here, not a tuning choice.
- **Relevance boundaries** — project, agent, workspace. These describe *where a
  belief was learned*, which is evidence about where it applies, not permission to
  see it. They are ranking and rendering inputs.

Project scope moves to the second category. Within a principal, every belief is
eligible everywhere; `scope_affinity` decides how strongly it competes.

### Not everything transfers equally

Carrying beliefs across projects introduces a failure mode that scope isolation
used to prevent for free: **false transfer** — stating project A's facts as though
they hold in project B. The defense is that portability is a property of the belief
type, set at formation and carried on the belief.

| Class | Examples | Carries |
| --- | --- | --- |
| **Portable** | preferences, working style, communication norms, standing constraints, skills, corrections to the agent's own knowledge | fully, at full weight |
| **Contextual** | technology choices, architectural patterns, decisions and their rationale | yes, always attributed to the project it came from |
| **Local** | environment endpoints, service names, credential locations, current status, deadlines, per-project overrides | only on explicit query, heavily discounted, always attributed |

A belief recalled outside the project it was learned in is **rendered with its
origin** and applied at a **reduced confidence band**, so it can never read as an
unqualified fact about the current project:

```text
[m:44b9] (learned in veetbot) Postgres with pgvector backs memory.
```

### Corroboration across projects promotes

The mechanism that turns this from a wider candidate pool into actual learning is
**promotion**. When formation observes the same belief independently in two or more
distinct project scopes, it promotes the belief from project scope to `user` scope:
independent corroboration in unrelated contexts is precisely the evidence that a
belief is about the principal rather than about one project. A promoted belief drops
its origin attribution, competes at full weight everywhere, and becomes eligible for
the session-open snapshot.

The write-path rule is specified in
[memory formation and consolidation](memory-formation-and-consolidation.md). The
read path's obligation is to make it observable: cross-project recalls are marked in
the trace, so a belief that keeps proving useful outside its origin is visible as a
promotion candidate.

An explicit per-project override always outranks a carried belief. Learning globally
does not mean ignoring local instruction.

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

- `tenant_id` and `principal_id` — the isolation boundary;
- lifecycle: exclude `superseded` and `expired` unless the query is historical;
- bi-temporal validity: `valid_from <= as_of AND (valid_to IS NULL OR valid_to > as_of)`,
  where `as_of` defaults to now. This is what makes "what did I believe last month" a
  first-class query rather than an archaeology exercise;
- sensitivity ceiling for the current surface (a memory that is fine in a private
  session may not be renderable to a shared or inbound surface — Section 22);
- `portability = local` beliefs from other projects, unless the query names their
  subject explicitly. This is the one place project scope still narrows the
  candidate set, and it is a precision measure rather than an isolation one.

Project scope is otherwise **not** a predicate — it is carried into ranking as
`scope_affinity`. The isolation predicates are, and a filter on those implemented as
a rank feature is a defect rather than a tuning choice.

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
    + w_scope     · scope_affinity(b, q)  # origin match x portability
    + w_utility   · utility(b)            # usefulness when recalled
    - w_penalty   · penalty(b)            # stale, flagged, duplicate
```

with:

- `conf(b) = confidence × {active: 1.0, provisional: 0.4}` — provisional beliefs are
  retrievable but never dominant, and never enter the snapshot core;
- `reinforce(b) = log1p(corroboration_count) / log1p(C_max) × exp(-Δt / τ)`, where
  `Δt` is time since `last_reinforced_at` and `τ` varies by `belief_type` — stable
  preferences decay slowly, situational facts quickly;
- `authority(b)` from the belief's provenance: direct user statement, agent-affirmed
  conclusion, or inference;
- `scope_affinity(b, q)` combines origin match with portability. A belief learned in
  the current project scores highest; `user` and `global` beliefs score at parity with
  it, since they have already earned generality; a belief carried from another project
  is discounted by its portability class — lightly for `portable`, materially for
  `contextual`, heavily for `local`. Carrying is the default, not the exception; the
  discount only decides how hard it has to compete;
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
highest score first, stopping at the budget or the floor. That allocation is set by
the budget allocator in [the context engine](context-engine.md): the frozen snapshot
is its Region A half and in-turn recall its Region B half, and in-turn recall is the
**first** class to yield when the body is over budget. Then render a
**deterministic, trust-labeled block**:

```text
<memory as_of="2026-07-24T09:00:00Z" policy="retrieval@3">
  [m:8f21] Andy prefers concise prose-first writing.  (user-stated, high)
  [m:1c07] Andy's current project is veetbot.         (user-stated, high)
  [m:44b9] Andy's stack is Postgres-backed.           (inferred, medium)
  [m:7ea5] (learned in atlas) Deploys are gated on CI. (inferred, low)
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
- **Carried beliefs are attributed.** A belief recalled outside its origin project
  renders with `(learned in <project>)`. Attribution is part of the data, not a
  footnote — an unattributed carried belief is a false-transfer bug.
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
- **Cross-project usefulness is a promotion signal.** A belief carried out of its
  origin project and then actually used is recorded in `carried_in`, giving formation
  read-path evidence of generality alongside its own write-path corroboration.

## Ports and data model

```python
class RecallQuery(BaseModel):
    tenant_id: str                     # isolation boundary, hard filter
    principal_id: str                  # isolation boundary, hard filter
    current_scope: str                 # relevance anchor, not a filter
    text: str | None = None            # intent; may be multi-sentence
    subjects: list[str] = []           # structured anchors, aliased
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
    origin_scope: str                  # where it was learned
    portability: str                   # portable | contextual | local
    carried: bool = False              # recalled outside origin_scope
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
    turn_id: UUID | None               # joins the answer to its evidence
    moment: str                        # snapshot | in_turn | child_run
    query: RecallQuery
    surface_id: str                    # ceiling in force at render time
    sensitivity_ceiling: str
    rendered_sha256: str               # binds the trace to exact bytes
    arm_latencies_ms: dict[str, int]   # per-tier; nulled after window
    candidates: int                    # operator tier
    returned: list[UUID]
    cited: list[UUID]                  # ids the model cited -> "used"
    dropped_for_budget: list[UUID]     # operator tier
    blocked: list[UUID]
    carried_in: list[UUID]             # promotion candidates
    retrieval_policy_version: str
    created_at: datetime
    operator_fields_expire_at: datetime
```

The user-safe projection is a separate read model, never a second write:

```python
class TracedBelief(BaseModel):
    belief_id: UUID
    subject: str
    statement: str
    learned_at: datetime               # first observed, not last touched
    origin_scope: str
    carried: bool
    authority: str                    # user-stated | affirmed | inferred
    source_episode_id: UUID | None     # "show me where this came from"
    confidence_band: str
    used: bool                         # model cited it this turn

class RecallTraceView(BaseModel):
    turn_id: UUID
    moments: list[str]                 # which recalls fed this turn
    beliefs: list[TracedBelief]
    considered_not_shown: int          # count only, never ids
    withheld_by_safety: int            # count and reason, never content
    as_of: datetime
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
    async def search(
        self, query: EpisodeQuery
    ) -> list[EventEnvelope]: ...

class TraceStore(Protocol):
    async def record(self, trace: RecallTrace) -> None: ...
    async def for_turn(self, turn_id: UUID) -> list[RecallTrace]: ...
    async def user_view(
        self,
        turn_id: UUID,
        viewing_surface_id: str,       # ceiling is min(recall, viewing)
    ) -> RecallTraceView: ...
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

## The trace is a user-facing surface

The `RecallTrace` has two consumers: the operator tuning ranking, and the user asking
why the agent said what it said. Both read the **same record**. Two logs would drift,
and the one shown to the user is the one that must not be wrong.

### What a trace can honestly claim

A trace records what was **placed in context**, not what the model attended to. "Why
did you say that" overclaims; the honest question it answers is *what did you know
when you said that*. Where the model cited a belief id, the view marks that belief
**used**; everything else rendered is marked **available**. Inferring causal influence
from mere presence in the block would be fabrication dressed as transparency, so the
surface does not do it.

The trace is therefore **recorded, not reconstructed**. It is written in the same pass
that builds the rendered block and carries `rendered_sha256` over those exact bytes.
Answering the question later by re-running retrieval would return a different set —
policies version, beliefs supersede, decay moves scores — and a plausible re-run is
worse than no answer at all.

### The user-safe projection

The view for a turn is that turn's in-turn recall trace plus the session-open snapshot
trace, each marked with its moment, since both were in front of the model.

Included, per rendered belief: id, statement, and subject; **when** it was learned and
**where** — origin project, and whether it was carried; **how** — user-stated,
affirmed, or inferred — with a link to the source episode; its confidence band; and
whether it was used or merely available.

Included in aggregate: that *n* further beliefs were considered and not shown, and
that *n* were withheld by the injection scan. A silent withholding is worse than a
visible one, so blocked items are counted and explained, never displayed.

Excluded: arm latencies, per-arm ranks, raw scores, candidate ids, and policy
internals. To a user these are noise at best and alarming out of context — and the
individual beliefs that lost the ranking never reached the model, so correcting them
here would be correcting something that had no bearing on the answer.

Browsing everything the agent believes is a **different surface**, the memory
inspector of the formation spec. This one explains a single answer.

### Sensitivity is the stricter of two ceilings

A trace may be read on a different surface than the recall was rendered for. The view
is filtered by the **minimum** of the recall surface's sensitivity ceiling and the
viewing surface's, never either alone. Filtering only by the viewing surface would let
a restricted channel's recall be re-read at full sensitivity somewhere permissive;
filtering only by the recall surface would show, inside a locked-down surface, what a
permissive one had been allowed to see. Transparency must not become a disclosure
path.

### Retention is two-tier over one record

The operator fields — arm latencies, candidate counts, dropped ids — are high-volume
and useful only inside the tuning window, and are nulled on a schedule. The user-safe
fields ride with the session they explain and are deleted with it, on the same delete
path, so deleting a conversation deletes its explanations.

### Rejection is the correction path

Every item in the view carries its belief id, so "this is wrong" is unambiguous about
*which* belief. It is ambiguous about *what* is wrong, and the surface asks rather
than guesses:

- **Not true** — retire the belief.
- **Was true, has changed** — supersede it with the correction.
- **True elsewhere, not here** — a scope and portability correction, not a
  retirement. This kind of rejection exists only because beliefs carry, and it is the
  highest-value signal for tuning the portability classes.

An undifferentiated rejection flags the belief for review and down-weights it; it does
not retire it. Retiring on an ambiguous signal destroys information, for the same
reason formation refuses to silently overwrite. Rejections are durable events, so
**re-derivation must replay them** — a rejected belief that reappears because the
consolidation policy was upgraded is the worst bug this surface can have. The
write-path handling is specified in
[memory formation and consolidation](memory-formation-and-consolidation.md).

## Failure modes and defenses

| Failure | Defense |
| --- | --- |
| Context dilution from over-retrieval | hard budget, relevance floor, per-subject cap; measured as noise ratio |
| Stale belief surfaces as current | `as_of` filtering, supersession collapse, time-decayed reinforcement |
| Poisoned memory replayed every session | load-time injection scan to `[BLOCKED]`; memory is data; policy is unreachable from memory |
| Prompt cache invalidated by memory | frozen byte-stable snapshot; all mid-session recall into the user turn |
| Cross-tenant or cross-principal leak | isolation scope is a SQL predicate, never a feature; asserted per query path in tests |
| False transfer — project A's facts asserted about project B | portability classes, origin attribution on every carried belief, reduced confidence band until corroborated locally, explicit local overrides outrank carried beliefs |
| High-value item buried mid-block | small blocks, highest score first, task recall adjacent to the goal |
| Retrieval latency on the critical path | concurrent arms with a shared deadline, partial-result degradation, precomputed snapshot |
| Wrong belief entrenched by being useful | usage resets decay but never raises confidence |
| Trace disagrees with what the model actually saw | traces are recorded in the render pass, never reconstructed; `rendered_sha256` binds the trace to the exact bytes |
| Trace becomes a disclosure path | the view is filtered by the minimum of the recall surface's ceiling and the viewing surface's; blocked items are counted, never shown |
| A rejected belief returns after re-derivation | rejections are durable events that re-derivation replays, not a delete applied to a projection |

## Hard gates

Retrieval evaluation shares the harness with formation (Section 20), and building it
is what finally makes formation measurable end to end — formation quality cannot be
observed until beliefs can be read back.

1. **Currency** — after a preference change, retrieval must return the new belief
   and never the superseded one. **M9.**
2. **Historical correctness** — an `as_of` query returns what was believed
   then. **M9.**
3. **Injection resistance** — a poisoned belief renders `[BLOCKED]` and does not
   alter behavior. **M9.**
4. **Scope isolation** — zero cross-tenant and cross-principal results. A hard
   gate, not a metric to improve. **M9.**
5. **Trace faithfulness** — for sampled turns, the beliefs listed in the trace
   must reproduce the rendered block that the recorded hash covers. A hard gate,
   not a metric to improve. **M9.**
6. **View ceiling** — no belief above `min(recall ceiling, viewing ceiling)` may
   ever appear in a view. A hard gate, not a metric to improve. **M9.**
7. **Correction durability** — a rejected belief does not return, including across
   a consolidation-policy upgrade and a full re-derivation. **M9.**
8. **Cache preservation** — measured cached-token ratio must not regress when
   memory is enabled. This is the invariant's regression test. **M9.**
9. **No triple regression** — retrieval improves target eval cases **without**
   increasing policy failures, without regressing cache utilization, and without
   raising noise ratio. **M9.**

Gates 5 and 6 are one sentence in the original list, split because that sentence
says *"Both are hard gates"* and the registry needs one identifier per gate.

## Tracked metrics

- **Consequential recall@k** — of the facts a later task needs, how many are retrieved
  within budget.
- **Noise ratio** — retrieved-and-irrelevant over retrieved. The counterweight to
  recall; both are reported, never recall alone.
- **Transfer precision and transfer lift** — the paired metrics for carrying beliefs
  across projects. Lift: tasks in a new project that succeed because something learned
  elsewhere was recalled. Precision: carried beliefs that were correct in the new
  context, and attributed when they were not certain. Reported together, since raising
  either alone is trivial and worthless.
- **Cost and latency** — retrieval tokens per turn and p95 recall latency within budget.
- **End-to-end lift** — LOCOMO-style multi-session scenarios, the metric that justifies
  the whole layer.

## Build sequence (incremental, each gated by evals)

1. **Deterministic core.** Structured scope-filtered lookup, session-open snapshot,
   byte-stable rendering, trust labeling, and traces — recorded with their user-safe
   fields and `rendered_sha256` from the first commit, since retrofitting faithfulness
   onto a trace that was never written faithfully is not possible. No ranking beyond
   confidence and recency. This alone gives the agent a stable user model.
2. **Lexical recall and ranking.** Postgres FTS arm, the hand-weighted ranker, the
   relevance floor, budgeted assembly, supersession collapse.
3. **In-turn recall.** `memory.search`, automatic pre-turn recall with a floor, and the
   recall delta over the frozen snapshot.
4. **Feedback loop.** Usage tracking into decay resistance and `utility`; recall misses
   into formation re-derivation hints.
5. **Inspectable trace and correction.** The `RecallTraceView` projection, the
   two-ceiling filter, two-tier retention, and the typed rejection path into
   formation. The record is written from step 1; this step exposes it.
6. **Semantic arm.** `pgvector` plus RRF fusion — only if the harness shows lift over
   lexical (Milestone 9).
7. **Later arms.** Entity-graph expansion (graph spec), external provider arm
   (ADR-0014), learned reranker once traces provide labeled data.

## Decisions

- **Retrieval happens at distinct moments with distinct profiles**: a frozen
  session-open snapshot in the cached prefix, in-turn recall into the user turn, and
  separately-scoped child-run recall. The prefix is never rewritten mid-session.
- **Isolation scope is a filter; relevance scope is a ranking feature.** No score
  crosses a tenant or a principal. Project scope is not an isolation boundary.
- **Beliefs carry across projects by default.** The agent learns from every project
  and environment; a belief learned in one is eligible everywhere for that principal,
  discounted by its portability class, rendered with its origin, and outranked by any
  explicit local override. Independent corroboration in a second project promotes the
  belief to `user` scope, where it drops attribution and competes at full weight.
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
- **The snapshot is capped by item count first and tokens second, never by a pure
  percentage of the window.** It starts at 40 items / 1,500 tokens for an interactive
  session, doubles for long-running async runs that amortize it, and is expected to
  *shrink* as the query former and ranker improve. Utilization and miss rate from the
  trace are the tuning signals.
- **The trace has two consumers and one record.** It is a user-inspectable surface as
  well as an operator artifact: recorded rather than reconstructed and bound to the
  rendered bytes, projected user-safe, filtered by the stricter of the recall and
  viewing ceilings, retained on two clocks, and carrying a typed rejection path back
  into formation. It claims only what was known at the turn, never what the model
  attended to.

## Open questions

None outstanding for the read path. The snapshot budget is now a default with a
measured tuning loop (see [Sizing the snapshot](#sizing-the-snapshot)) rather than an
open decision. The temporal entity graph, which supplies the later graph-expansion arm,
is not yet specified.
