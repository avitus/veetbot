---
title: Memory Formation & Consolidation
status: design
canonical: true
---

# Memory formation and consolidation

This document specifies the **write path** for long-term memory: how raw episodes
become durable, curated beliefs and an evolving model of the user. It is the
detailed design behind Milestone 9 (see the
[engineering plan](engineering-plan.md)) and is recorded as
[ADR-0018](../adr/0018-memory-formation-and-consolidation.md).

Scope: **formation** only. Representation details beyond what formation needs and
the entity-graph layer are separate specs; the read path is specified in
[memory retrieval and ranking](memory-retrieval-and-ranking.md). This document
assumes the storage primitives from Milestone 9 (the `MemoryRecord` belief store)
and the episodic event log from Section 6.8.

## Why formation is the hard part

Retrieval is a ranking problem with well-understood tools. Formation — deciding
what is worth remembering, in what form, and how it reconciles with what is
already believed — is where memory quality is actually won or lost. A wrong or
stale belief is worse than a missing one, because the agent will act on it
confidently. This spec optimizes for **precision, provenance, and reversibility**
over raw capture.

## Principles

- **Memory is a governed projection over the episodic log.** The append-only
  event log (Section 6.8) is ground truth. Beliefs are *derived* from it, never a
  lossy side-channel. This makes memory re-derivable, auditable, and correctable
  (see "Re-derivation" below).
- **Formation is deliberate and gated**, never an implicit effect of reading
  content. Untrusted content can never *directly* create a belief.
- **Precision over recall.** Prefer to miss a fact than to fabricate one.
- **Everything is provenance-linked, user-visible, and reversible.** Every belief
  points back to the events and the consolidation run that produced it; the user
  can inspect, edit, and delete; edits and deletes are themselves events.

## Memory states and tiers

Two different things get called "tiers"; the design keeps them as separate axes.

**Confidence lifecycle (how established a belief is).** This is a continuous
`confidence` score, not a stack of discrete tiers. The named states are thresholds
and lifecycle transitions over that score:

- `candidate` - proposed in the pipeline, not yet committed.
- `provisional` - committed but low-confidence, on probation; contributes weakly to
  retrieval; promotes to `active` when reinforcement pushes confidence and
  corroboration over a threshold, or decays out if never seen again.
- `active` - established and durable; full retrieval weight.
- `superseded` - replaced by a newer belief; retained (bi-temporal), excluded from
  live retrieval.
- `expired` / `retired` - past validity or decayed below the floor; retained for
  audit and re-derivation, excluded from live retrieval.

Keeping confidence continuous (with a couple of thresholds) rather than many hard
tiers means granularity can be tuned later by moving thresholds - no schema change
and no extra classification burden on the extractor. Start with
provisional -> active -> retired; add finer bands only if evals show a need.

**Memory hierarchy (where a memory lives).** Orthogonal to confidence, and the
tiering that most shapes long-term memory building. Information flows *up* as it is
consolidated and *out* as it decays:

| Tier | Store | Role |
| --- | --- | --- |
| Working | in-context (Section 7 working state) | the current task; hottest, ephemeral |
| Episodic | event log (Section 6.8) | what happened; recent, high-volume, cheap |
| Semantic (long-term) | belief store | consolidated model of the user and world |
| Archival | cold episodes | retained for re-derivation; not routinely retrieved |

**Formation is the promotion mechanism from episodic to semantic**; decay and
forgetting demote or evict; [retrieval](memory-retrieval-and-ranking.md) pages
across the tiers into working memory. A belief always lives in the semantic tier -
its confidence state is the separate axis above.

## The pipeline

Formation is an incremental pipeline from episode to committed belief. Stages 1–7
run per consolidation; stage 8 is background maintenance.

```text
episodic log ─▶ 1 trigger ─▶ 2 segment & select ─▶ 3 extract candidates
     ▲                                                     │
     │                                                     ▼
 8 decay / re-derive ◀─ 7 commit ◀─ 6 gate ◀─ 5 resolve ◀─ 4 filter
```

### 1. Trigger — when consolidation runs

Tiered, so the expensive stages run rarely:

- **Turn-boundary (cheap flag).** After a run completes, a lightweight check
  decides *whether* anything memory-worthy happened. It does not extract; it only
  marks the session for consolidation. Avoids running the extractor every turn.
- **Session-boundary / idle (primary).** When a session ends or goes idle, run
  full consolidation over that session's new episodes. This is the main cadence —
  the analogue of sleep consolidation.
- **Explicit.** The agent's `memory.remember` tool, or the user saying "remember
  that…". High precision; skips the salience gate but still goes through resolve,
  gate, and commit.
- **Scheduled reconsolidation.** A periodic background job revisits, merges,
  decays, and can re-derive (stage 8).

Cadence is configured per agent / policy profile. Default: session-boundary +
explicit + scheduled, with turn-boundary as a cheap flagger only.

### 2. Segment & select

- Operate only over episodes **after the consolidation watermark** — a per-session
  cursor recording the last consolidated event sequence. This makes formation
  incremental and idempotent (re-running consolidates nothing new).
- Select salient spans: user corrections, decisions, stated preferences, new
  entities, explicit "remember this", task outcomes. De-prioritize routine tool
  chatter.
- **Working state is a second input.** `WorkingState.established_facts` surviving to
  the session boundary are offered as candidates with their provenance and trust
  level attached ([context engine](context-engine.md), ADR-0020). They are
  candidates, not beliefs: they enter here and pass through every stage below,
  including the trust gate. A fact the run derived from untrusted content is still
  untrusted.
- **Trust gate at selection.** Spans that are solely `EXTERNAL_UNTRUSTED`
  (tool output, web content) are never a *direct* formation source (Section 11.2).
  Such content can only become memory once the user or the agent affirms it; it is
  evidence, not a source. This is the primary prompt-injection defense on the
  write path.

### 3. Extract candidates

A consolidation model (a cheap aux tier — most consolidation is small) reads the
selected episodes plus a **compact view of related existing beliefs**, so it
proposes *deltas*, not duplicates. It emits **candidate memories** with
schema-constrained structured output (Section 10 / structured outputs):

```python
class MemoryCandidate(BaseModel):
    belief_type: str          # "fact" | "preference" | "relationship"
                              # | "user_model_attr" | "procedure_pointer"
    subject: str              # the entity the belief is about
    statement: str            # the asserted content
    polarity: str             # "assert" | "retract"
    source_event_ids: list[int]   # mandatory provenance
    model_confidence: float
    proposed_scope: str       # "user" | "project" | "global"
    proposed_portability: str # portable | contextual | local
    sensitivity_guess: str
    valid_from: datetime | None
    expires_hint: datetime | None
```

Candidates are **proposals, not writes.**

**Portability has a deterministic ceiling.** Each `belief_type` carries a default
portability — preferences, user-model attributes, and procedure pointers are
`portable`; facts and relationships default to `contextual`. The extractor may
*lower* a candidate's portability but never raise it, so a model cannot make a belief
travel further than its type allows. Portability governs how a belief behaves outside
the project it was learned in, and is defined in
[memory retrieval and ranking](memory-retrieval-and-ranking.md).

### 4. Filter — eligibility and salience

- **Eligibility (hard gates, reject on any).** Derived solely from untrusted
  content; contains secrets/credentials/tokens; PII beyond policy; transient task
  detail; a restatement of platform instructions or private reasoning. This
  mirrors the "never automatically store" list in Milestone 9 and ADR-0006/0007.
- **Salience (soft ranking).** Keep candidates above a worth-remembering
  threshold: durability (will this matter next week?), specificity, corroboration,
  and user-signaled importance. Sub-threshold candidates are dropped or held in the **provisional** state (see "Memory states and tiers") that promotes to `active` only on later reinforcement.

### 5. Resolve against existing memory

For each surviving candidate, match against existing beliefs by subject + semantic
+ lexical similarity, then:

| Relationship | Action |
| --- | --- |
| New | Insert as a new belief. |
| Duplicate / near-duplicate | **Reinforce**: increment corroboration, bump confidence, append `source_event_ids`, update `last_reinforced_at`. No new record. |
| Refinement | Update/extend the existing belief (e.g. more specific). |
| Contradiction | **Never overwrite.** Insert the new belief and link `conflicts_with` / `supersedes`; apply the conflict policy below. |

**Conflict-resolution policy (default).** Resolve by **source authority, then
recency**: a direct user statement supersedes an inferred one; a more recent user
statement supersedes an older user statement. The superseded belief is retained
with `valid_to` set (bi-temporal — we keep *what was believed and when*), marked
`superseded`. Formation is **fully autonomous**: resolution is never blocked. Ambiguous or sensitive resolutions are still committed (by authority then recency) but marked `flagged_for_review` and surfaced to the user, who can correct or delete after the fact - the safety model is after-the-fact review, not a pre-commit gate.

Bi-temporal validity is what lets "Andy works at Acme" become false without being
deleted, and lets the agent answer "what did I believe last month".

**Promotion across scopes.** Matching is performed within the principal, not within
the project, so a candidate formed in one project matches an existing belief formed
in another. When the same belief is independently corroborated in **two or more
distinct project scopes**, it is **promoted** to `user` scope: independent
observation in unrelated contexts is the evidence that a belief describes the
principal rather than one project. Promotion sets `scope = "user"`, retains every
contributing `origin_scope` in provenance, and emits `memory.promoted`. This is the
write-path half of
[beliefs carrying across projects](memory-retrieval-and-ranking.md); the read path
supplies additional evidence by recording which carried beliefs proved useful outside
their origin.

Promotion applies only to `portable` and `contextual` beliefs. A `local` belief
(an endpoint, a service name, a deadline) that happens to look similar across two
projects is a coincidence, not a generalization, and is never promoted.

### 6. Gate — policy and safety

Each proposed write passes the deterministic policy engine, exactly like a tool
call (Section 9):

- Injection scan of the candidate statement.
- Sensitivity classification. Formation is fully autonomous, so sensitive writes are **not** held for confirmation; they are committed and marked `flagged_for_review` for the user. The deterministic *eligibility* gates in stage 4 (never form secrets, credentials, untrusted instructions, or PII-beyond-policy) are hard filters that always apply - autonomy governs confirmation, never those.
- Volume/rate caps, so one pathological session cannot flood memory.

### 7. Commit

- Write the belief(s) through the `MemoryStore` port in a short transaction.
- Emit a `memory.formed` / `memory.reinforced` / `memory.superseded` /
  `memory.needs_confirmation` event to the log — memory changes are themselves
  auditable episodes (and part of what makes re-derivation possible).
- Advance the consolidation watermark.
- If the belief is a `user_model_attr`, update the **user-model projection** (a
  curated view over user-scoped beliefs — the "deepening model of who you are").

### 8. Decay and re-derivation (background)

- **Decay.** Provisional and low-confidence beliefs that go unused lose confidence
  over time; expired beliefs (past `expires_at`) retire. Reinforcement resets
  decay — used, corroborated beliefs persist; noise fades.
- **Consolidation/merge.** Many related low-level beliefs compress into fewer
  higher-level ones, provenance retained.
- **Re-derivation ("re-remember").** Because the event log is ground truth, a
  scheduled or opt-in job can re-run formation over history with an improved
  extractor, prompt, or model, and reconcile — producing a better belief set
  **without losing auditability**. Every belief records the
  `consolidation_policy_version` that formed it, so we always know which run
  produced which belief. This capability is the main advantage of deriving memory
  from an episodic log rather than writing it as a lossy side effect.

## User corrections are evidence

Beliefs are inspectable, so they are also rejectable. A rejection arrives with the
belief id it refers to, from the recall trace the user was reading
([memory retrieval and ranking](memory-retrieval-and-ranking.md)), which makes *which*
belief unambiguous. What is wrong about it is not, and the surface asks rather than
guesses — the three answers are three different writes:

| The user says | Formation does |
| --- | --- |
| Not true | Retire the belief: `valid_to = now`, status `superseded`, no replacement. |
| Was true, has changed | Supersede it with the correction as a new belief at user authority. |
| True elsewhere, not here | Lower `portability` and record a negative scope override. The belief survives in its origin; it stops being carried here. |
| *(unspecified)* | Flag for review and down-weight. Never retire on an ambiguous signal. |

A rejection is **evidence at the highest authority** — a direct user statement, which
outranks anything inferred and any older user statement under the existing
conflict-resolution policy. It is not a special case in the resolver; it enters through
the same door as any other user statement.

**Rejections are events, not edits, and re-derivation replays them.** This is the sharp
edge. Because the event log is ground truth, a re-derivation run re-forms beliefs from
history — and would cheerfully re-form everything the user has ever corrected, since
the improved extractor sees the same original episodes. A rejection is therefore an
input to formation rather than a correction applied to its output: every consolidation
run, including re-derivation, applies the outstanding rejections for the principal
before commit.

That matching cannot be by belief id, because re-derivation mints new ids. A
`BeliefRejection` stores the rejected `subject`, `statement`, and `belief_type`, and
matching is by content similarity on the same comparison the resolver already uses for
duplicates. A belief that a user has rejected does not come back with a new id.

**Rejecting is not deleting.** "This is wrong" is a correction and keeps its content
for audit and replay. "Delete this" is a data-removal request: the belief record is
removed and the tombstone retains a **content hash** rather than the statement, so
re-derivation can still refuse to re-form it without the platform continuing to hold
what the user asked it to forget.

Rejection rate is also the cheapest formation-quality signal available — corrections
per hundred rendered beliefs, measured against real usage rather than a rubric.

## Data-model additions

Extends the Milestone 9 `MemoryRecord`:

```python
class MemoryRecord(BaseModel):
    # ... existing Milestone 9 fields (id, tenant_id, principal_id,
    #     scope,
    #     subject, statement, source_event_ids, confidence, sensitivity,
    #     valid_from, expires_at, status) ...
    belief_type: str
    polarity: str                       # "assert" | "retract"
    portability: str                    # portable | contextual | local
    origin_scopes: list[str]            # every scope corroborating it
    corroboration_count: int = 1
    last_reinforced_at: datetime
    # bi-temporal: when the belief stopped holding
    valid_to: datetime | None
    superseded_by: UUID | None
    # `status` (from Milestone 9) carries the lifecycle state:
    #   candidate | provisional | active | superseded | expired
    flagged_for_review: bool = False    # committed, surfaced for review
    formation_run_id: UUID              # which consolidation produced it
    consolidation_policy_version: str

class BeliefRejection(BaseModel):
    id: UUID
    tenant_id: str
    principal_id: str
    belief_id: UUID                     # as rejected; ids may change
    # kind: untrue | changed | not_here | unspecified
    kind: str
    # content keys survive re-derivation
    subject: str
    # statement is None once the user has asked for deletion
    statement: str | None
    statement_sha256: str               # the tombstone key
    belief_type: str
    scope: str                          # where the rejection was made
    replacement_id: UUID | None         # set for "was true, has changed"
    trace_id: UUID | None               # what the user was looking at
    created_at: datetime

class ConsolidationRun(BaseModel):
    id: UUID
    tenant_id: str
    principal_id: str
    # trigger: session | explicit | scheduled | ...
    trigger: str
    scope: str
    watermark_before: int
    watermark_after: int
    model: str
    policy_version: str
    candidates_proposed: int
    committed: int
    reinforced: int
    superseded: int
    rejected: int
    started_at: datetime
    finished_at: datetime | None
```

New relationships between beliefs: `conflicts_with`, `supersedes`.

## Ports and runtime placement

- **`MemoryStore` port**: `query`, `upsert_belief`, `reinforce`, `supersede`,
  `list`, `edit`, `delete`, `reject`, `outstanding_rejections`. Backends: Postgres
  (FTS + normalized belief tables) first; `pgvector` and an external provider (e.g.
  Honcho) later, behind the same port (ADR-0014).
- **`MemoryConsolidator` port**: `run(trigger, scope, since_watermark) ->
  ConsolidationResult`. The builtin implementation is LLM extraction as above; an
  external memory provider can be delegated to behind this port.
- **`Salience` and `ConflictResolver`** are replaceable strategies.
- **Runtime placement.** Consolidation runs as a **restricted child run**
  (subagent, Section 27.6 / Milestone 10) or a dedicated background worker job:
  tool set limited to memory read/write and retrieval, its own budget, sandboxed,
  returning a concise summary — the same governance model as the self-improving
  skills in Section 30. It never blocks the interactive turn.

## Governance and safety (cross-cutting)

- Formation is autonomous but **fully transparent and reversible**: every belief
  links to its source events and formation run; the user can list, inspect, edit,
  and delete; those actions are events.
- **Untrusted content can never directly form memory** — only user statements and
  the agent's own affirmed conclusions.
- **Never form** secrets, credentials, tokens, raw untrusted instructions, or
  private reasoning (ADR-0006/0007).
- **Formation is fully autonomous**: no belief requires synchronous human confirmation. Safety rests on the deterministic eligibility gates, the untrusted-content write ban, and after-the-fact transparency and reversibility; sensitive or ambiguous beliefs are committed but flagged for review.
- Every write is tenant- and principal-scoped.

## Hard gates

Formation cannot be tuned without measurement; build the harness alongside the
first formation layer (Section 20).

1. **Contradiction handling** — inject a preference change; verify supersession
   (not duplication) and that retrieval returns the current belief. **M9.**
2. **No fabrication** — it must not form beliefs unsupported by
   episodes. **M9.**
3. **Injection resistance** — an untrusted "remember X" must not form a
   belief. **M9.**
4. **Correction durability** — a rejected belief must not return, including after
   a full re-derivation under a newer consolidation policy. A hard gate. **M9.**
5. **No policy regression** — adapt LOCOMO-style long-horizon scenarios to
   exercise the write path. Gate: memory improves target eval cases **without**
   increasing policy failures. **M9.**

## Tracked metrics

- **Formation precision** — of committed beliefs, the fraction correct and
  worth keeping (rubric/graded). The primary metric.
- **Recall of consequential facts** — does it capture what later tasks need.
- **Rejection rate** — user corrections per hundred rendered beliefs. A precision
  proxy measured against real usage rather than a rubric.
- **Cost** — consolidation tokens per session within budget.

## Build sequence (incremental, each gated by evals)

The **builtin consolidation path is built to parity first** (steps 1-5); an external provider is a later comparison option (step 6), not the initial path.

1. Explicit `memory.remember` + belief store + provenance + user edit/delete +
   reinforce-on-duplicate. No automatic formation yet.
2. Session-boundary consolidation: extraction + eligibility gate + dedupe.
3. Conflict detection + supersession + bi-temporal validity.
4. Typed rejections and their replay, before re-derivation exists to violate them.
5. Decay + scheduled reconsolidation + re-derivation.
6. External-provider adapter option; user-model projection; graph edges (handed to
   the separate graph spec).

## Decisions

- **Formation is fully autonomous from the start.** No belief requires synchronous confirmation; safety is the deterministic eligibility gates, the untrusted-content write ban, and after-the-fact transparency and reversibility; sensitive or ambiguous beliefs are committed and flagged for review.
- **Build the builtin consolidation path to parity first.** An external provider (Honcho/Mem0-style) behind the `MemoryConsolidator` port is a later comparison option, not the initial path.
- **Keep the provisional tier, and model "tiers" as two axes** (see [Memory states and tiers](#memory-states-and-tiers)): a continuous confidence lifecycle (provisional -> active -> retired) and an explicit memory hierarchy (working -> episodic -> semantic -> archival) that formation promotes across. This is the tiered memory system - not merely two tiers.
- **The user model is a projection over user-scoped beliefs**, not a separately maintained artifact - one source of truth, no drift.
- **Beliefs are matched within the principal, not within the project, and corroboration across two or more project scopes promotes a belief to `user` scope.** The agent learns from every project and environment it works in. Portability is bounded by `belief_type` so that project-local facts never generalize.
- **Re-derivation is opt-in per principal**, not automatic on consolidation-policy upgrades - privacy-conscious users should not have old episodes silently re-mined.
- **User rejections are typed evidence that re-derivation replays.** A correction enters as a direct user statement through the ordinary resolver; it is stored as an event keyed by content rather than by belief id, so a rejected belief does not return with a new id after re-derivation. Rejecting is distinct from deleting: a deletion keeps only a content hash as its tombstone.

## Open questions

None outstanding for the formation loop. The next specs off this one raise their
own: [memory retrieval and ranking](memory-retrieval-and-ranking.md) is written and
carries none; the **temporal entity graph** is not specified and is deliberately
not scheduled for version 0.1. Milestone 9 delivers beliefs with scopes,
provenance, decay, and contradiction handling, which is what the retrieval path
consumes; an entity graph with valid-time and transaction-time edges is a second
storage model over the same evidence and would need its own consistency rule
against the belief store. It is recorded as post-0.1 work rather than as an open
question, so that nothing in Milestones 0 through 10 depends on it.

Retrieval also closes a loop back into formation: recalled beliefs that are used
resist decay, subjects that are repeatedly queried with no result queue targeted
re-derivation hints for stage 8, beliefs that prove useful outside the project they
were learned in are recorded as promotion candidates, and beliefs the user rejects
from a trace enter as typed corrections that every later run must honour.
