# ADR-0019: Memory retrieval and ranking

- Status: Accepted
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
4. **Isolation boundaries are hard filters; relevance boundaries are not.** Tenant,
   principal, lifecycle state, bi-temporal validity at `as_of`, and the surface
   sensitivity ceiling are SQL predicates applied before scoring. **Project scope is
   not an isolation boundary** and is not a predicate.
5. **Beliefs carry across projects by default.** The agent learns from every project
   and environment it works in. Within a principal, a belief learned anywhere is
   eligible everywhere, weighted by `scope_affinity` and by a **portability** class
   carried on the belief (`portable` / `contextual` / `local`, bounded by
   `belief_type` at formation). A belief recalled outside its origin project is
   **rendered with that origin**, applied at a reduced confidence band until
   corroborated locally, and outranked by any explicit local override. Formation
   **promotes** a belief corroborated in two or more distinct project scopes to `user`
   scope, where it sheds attribution and competes at full weight (ADR-0018). The
   accepted risk is **false transfer**, defended by portability classes, mandatory
   attribution, and the paired transfer-precision / transfer-lift evals.
6. **Recall is multi-arm and fused by reciprocal rank** (`k = 60`): structured and
   lexical (Postgres FTS plus trigram) always on; semantic (`pgvector`), graph
   expansion, and an external provider are additional arms behind the same fusion. RRF
   is chosen so arms need no cross-calibration and can be added or removed
   independently.
7. **Ranking is an explicit hand-weighted function** over match, confidence × state,
   time-decayed corroboration, source authority, scope affinity, historical utility, and
   penalties — with two weight vectors (`core`, `task`) over one function, versioned as
   `retrieval_policy_version`. A learned reranker waits for labeled trace data.
8. **Superseded and expired beliefs are excluded from live recall**, but genuinely
   unresolved conflicts are **surfaced as conflicts** rather than silently resolved at
   read time. Provisional beliefs are retrievable, down-weighted, and never enter the
   snapshot core.
9. **Rendering is deterministic and byte-stable**: total order by `(score, id)`,
   confidence bands rather than floats, no volatile fields, ids exposed for citation and
   correction, and the framing instruction held in the stable prefix rather than in the
   data block.
10. **A relevance floor governs, not the budget.** Candidates below the floor are dropped
    even when budget remains; filling the context is not the objective.
11. **Retrieval enforces the memory trust boundary**: items are `TrustLevel.MEMORY` data,
    injection-scanned at load with per-item `[BLOCKED]` placeholders, sensitivity-redacted
    per surface, and unable to affect policy or approvals.
12. **Usage feedback resets decay but never raises confidence.** Citation extends decay
    resistance and `utility`; only evidence from the world changes confidence. This
    prevents a wrong belief from entrenching itself by ranking well.
13. **Recall misses are a formation signal**: repeatedly querying a subject and returning
    nothing above the floor queues a targeted re-derivation hint for the formation loop.
14. **Every recall emits a `RecallTrace`**, which is both the Milestone 9 retrieval-trace
    requirement and the tuning and feedback channel.
15. **The trace is a user-facing surface with a second consumer, over one record.** The
    operator view and the user view are projections of the same `RecallTrace`; two logs
    would drift and the user-facing one is the one that must not be wrong. It is
    **recorded in the render pass, never reconstructed**, and bound to the rendered
    bytes by `rendered_sha256`. The user-safe projection (`RecallTraceView`) carries what
    was known — statement, when and where learned, authority and source episode,
    confidence band, and whether the model cited it — and excludes arm latencies, scores,
    candidate ids, and policy internals, reporting dropped and blocked items as counts
    only. It is filtered by the **minimum of the recall surface's and the viewing
    surface's sensitivity ceiling**. Retention is two-tier over one record: operator
    fields expire on the tuning window, user-safe fields live and die with their session.
    The trace claims only what was in context, never what the model attended to.
16. **Rejection from a trace is a typed, first-class formation input**: not true (retire),
    was true and changed (supersede), or true elsewhere but not here (a portability and
    scope correction). An unspecified rejection flags and down-weights but never retires.
    Rejections are durable events that re-derivation must replay (ADR-0018).
17. **The session snapshot is sized by an absolute item and token cap, not a percentage
    of the context window.** Default `core` budget: **40 items / 1,500 tokens** for
    interactive sessions, 80 / 3,000 for long-running async runs, and 15 / 500 for child
    runs, each additionally bounded by 2% of the model's window. The **item cap is
    primary** — dilution tracks the absolute number of irrelevant items rather than the
    fraction of the window they occupy, so a larger window is not a reason for a larger
    snapshot — and the token cap is the backstop. Roughly two-thirds of the item budget
    is reserved for durable user-model and preference beliefs, the remainder for the
    opening-goal priming set. The budget is expected to **shrink** as the query former
    and ranker improve, tuned against snapshot utilization and snapshot-miss rate drawn
    from `RecallTrace` rather than reset by hand.

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
- Experience compounds across projects: the second project starts from what the first
  taught the agent, and the promotion rule turns repeated cross-project observation
  into a durable user model rather than a pile of per-project duplicates.
- In exchange, project isolation is no longer free. False transfer becomes a standing
  risk carried by portability classes, attribution, and evals rather than by a
  predicate, and the belief store must be indexed for principal-wide matching rather
  than per-project matching.
- The trace store stops being an internal debugging artifact and acquires a
  user-visible contract. Its schema, its retention, and its redaction rules are now
  product surface, and the record must be written correctly on the first commit —
  faithfulness cannot be retrofitted onto a trace that was never recorded faithfully.
- Retention gets more complicated in exchange for staying honest: operator fields
  expire on the tuning window while user-safe fields live with their session, which is
  a nulling job over one table rather than two stores that could disagree.
- The read path acquires a write path back into formation. Corrections made from a
  trace are typed events, so retrieval is no longer purely a consumer of memory, and
  ADR-0018's re-derivation must replay them or silently undo the user's work.
- Two new hard gates enter the eval harness: trace faithfulness (the recorded trace
  matches the rendered bytes) and correction durability (a rejected belief does not
  return after re-derivation).
- The read path has no open questions left, so Milestone 9 can be scheduled without a
  pending sizing decision. The snapshot budget ships as a default with an instrumented
  shrink path rather than as a constant to be argued about later, which means the
  utilization and miss-rate signals must exist in the trace before the number can be
  tuned — they do, as a consequence of decision 15.

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
- **Project scope as a hard isolation boundary** (the original proposal): rejected. It
  makes false transfer impossible, but at the cost of relearning the same lessons in
  every repository — the agent would be permanently new. Isolation is retained where it
  is a security property (tenant, principal, sensitivity) and dropped where it was only
  a proxy for relevance.
- **Unconditional carry with no portability classes**: rejected; without them a
  deployment endpoint or a service name from one project is asserted as fact in
  another, which is the fastest way to make a memory layer untrustworthy.
- **Copying beliefs into each project on first use**: rejected; duplicates diverge,
  supersession has to be applied N times, and the corroboration signal that drives
  promotion is destroyed by the copies. One belief, many origins, is the correct shape.
- **An operator-only, fire-and-forget trace with short retention** (the original
  proposal): rejected once the trace became a user surface. Short retention answers
  "why did you say that" only for the questions asked within the tuning window, which
  is not when users ask them.
- **A separate user-facing explanation log alongside the operator trace**: rejected;
  two records of the same event drift, and the one that drifts is discovered by a user
  being told something untrue about their own data. One record, two projections.
- **Reconstructing the explanation on demand by re-running retrieval**: rejected;
  ranking weights, decay, supersession, and the belief set itself all move, so a re-run
  returns a different set and describes a turn that never happened. A plausible
  reconstruction is worse than no answer, because it is believed.
- **Inferring which beliefs the model actually used**: rejected as unknowable. The
  trace claims presence in context and citation, both observable, and declines to claim
  influence, which is not.
- **A single "this is wrong" rejection button**: rejected; "wrong" conflates a false
  belief, a stale one, and a true one that does not apply here, which are three
  different writes. An unspecified rejection is still accepted, but it flags rather
  than retires.
- **Sizing the snapshot as a fixed percentage of the context window**: rejected;
  dilution tracks the absolute count of irrelevant items, so a larger window would
  silently buy a larger and worse snapshot. The percentage survives only as a ceiling
  for small windows.
- **One snapshot budget for every session type**: rejected; a long-running async run
  amortizes one fixed block over many requests and knows its objective at the start,
  while a short interactive session pays a much larger share of its total tokens for
  the same block.
- **Growing the snapshot as memory accumulates**: rejected; the snapshot should shrink
  as the query former and ranker improve, not grow with the belief count. A memory
  layer that gets slower and noisier the longer it is used is the failure this budget
  exists to prevent.
