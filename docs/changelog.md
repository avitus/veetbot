---
title: Changelog
---

# Changelog

## 2026-07-25 — Model gateway and the provider-neutral protocol

- Added `docs/plan/model-gateway.md`, the translation design for Section 10 and
  Milestones 1 and 3: the normalized stream, the two first adapters, routing,
  usage and cost, retries, and reasoning. Recorded as ADR-0002, constrained by
  ADR-0006, ADR-0007, ADR-0010, and ADR-0012.
- Defined the roughly twenty types the plan used at call sites and never
  declared: the five `ConversationItem` members, `ContentPart` with its text,
  image and file variants, `PendingToolCall`, the six streaming event classes,
  `ModelUsage`, `CostSource`, `StopReason`, `ModelAttempt`, the three error
  classes, `FakeModelScript` with `ScriptedTurn` and `ScriptedToolCall`, the
  `UsageRepository` port, and `ResolvedModel` with `ModelCapabilities`,
  `ModelLimits`, and `ModelPricing`.
- Stated the **six invariants of the normalized stream** — contiguous sequence,
  exactly one terminal event, contiguous ordered deltas per item, `call_id` and
  `name` known at tool-item start, advisory usage, and no raw provider error
  text or credentials on any event — and gave them a shared validator, so a
  violation is an adapter defect rather than a caller concern.
- Made **one shared assembler** fold events into turns for every adapter, which
  is what makes the contract suite a controlled comparison: the same code
  produces the turn on every provider, so a difference in the turn is a
  difference in the events.
- Resolved the **nine unfilled cells** of the Section 10.2 mapping table without
  changing any mapping it already states: `server_tool_use` becomes a protocol
  error, `content_block_stop` and `message_stop` are structural, `UsageEvent` is
  advisory so its missing OpenAI source stops mattering, `response.incomplete`
  maps to `MAX_TOKENS` where the cap is the cause, and the five OpenAI lifecycle
  events drive item bookkeeping only.
- Gave in-band `<think>` the mapping table ADR-0012 assumed and Section 10 never
  wrote, plus a streaming scrubber with one-token lookahead and a per-profile
  configurable tag pair, since open models do not agree on the delimiter.
- Gave `cache_creation_input_tokens` the home Section 10.2 said to find for it:
  `cache_write_input_tokens`, a **fifth tracked token class** on both
  `ModelUsage` and `RunUsage`. Made `reasoning_tokens` `None` rather than `0`
  where a provider does not itemize it, and moved the itemization question into
  pricing as `reasoning_priced_separately`.
- Added the **`ModelRouter` port**, turning `model_policy` from a bare string
  into a `ResolvedModel` carrying provider, model, capabilities, limits,
  pricing, and a credential reference. This gives `ModelCapabilities` a
  resolution path and gives the context engine's "8,192 or the model's default"
  output reserve its missing second half.
- Resolved **provider pinning against availability routing** temporally rather
  than architecturally: selection happens once at run start, the pin is absolute
  and persisted for the life of the run, and Milestone 10 routes selection and
  never live runs.
- Split **retry ownership on `stream_had_output`**: the adapter retries only
  before the first event reaches the caller, at most three times; after any
  output it fails and the caller decides. `max_attempts = 3` lives in
  application code, matching the worker's existing figure.
- Added the **model-call timeouts** no document carried — `timeout_seconds` and
  the load-bearing `stream_idle_seconds` — and made cancellation produce
  `StopReason.CANCELLED` on a partial turn rather than an error.
- Bounded the **`PLATFORM` trust default** on `ProviderReasoningItem` with four
  properties that leave the label no consumer able to act on it: the payload is
  never parsed, never rendered as prompt text, never reaches the policy engine,
  and never enters memory or a user-facing renderer. The name is still wrong and
  is raised for review rather than edited.
- Made the gateway enforce **tool call and tool result pairing before sending**,
  which the policy spec's denial-as-tool-result and the context engine's
  compaction atomicity both depended on and neither owned.
- Added `model_calls` and `model_prices` to the schema, one row per attempt and
  an append-only price history, so a three-month-old invoice stays reconcilable
  and per-attempt cost is a query rather than log archaeology. `runs.usage` is
  unchanged in shape and becomes a rollup maintained in the same transaction.
- Made **failed attempts count against budget**, checked before each attempt, so
  a crash-looping run stops for a stated reason instead of being mysteriously
  expensive.
- Added event payloads for `model.request.started` and `model.response.completed`
  that the context engine already consumed and Section 6.8 never specified, plus
  three telemetry attributes for the cached, cache-write, and reasoning token
  classes and a `model.attempt` span.
- Specified ten hard gates, six tracked metrics, and a fourteen-step build order
  in which the stream validator and the contract suite are written **before the
  first real adapter**.
- Wrote ADR-0002 with twenty decisions and seventeen rejected alternatives, and
  wired it and the spec into the navigation, the ADR index, and Section 10,
  Section 15, and Milestones 1 and 3 of the engineering plan.
- Recorded twelve judgment calls in `docs/status/questions-for-review.md`,
  flagging the `PLATFORM` trust default as the one worth reading first.
- No product implementation was performed.

## 2026-07-25 — Policy engine and approval lifecycle spec

- Added `docs/plan/policy-and-approvals.md`, the authorization design for
  Sections 8.3, 8.4, 9, 11.2, 13, and 22 and Milestone 4: classification, the
  deterministic matrix, the hardline layer, and the approval lifecycle. Recorded
  as ADR-0005 and ADR-0006, constrained by ADR-0017.
- Defined the five types the plan named as field types but never declared —
  `SideEffectClass`, `RiskLevel`, `IdempotencyClass`, `ProposedAction`, and
  `ApprovalStatus` — deriving each value from an existing plan statement rather
  than inventing a taxonomy: fifteen side-effect classes against Section 9.2's
  fifteen action categories, four idempotency classes against Section 8.4's four
  crash-recovery bullets.
- Rekeyed Section 9.2's matrix on `SideEffectClass` instead of a prose "action
  category" with no referent, and added a **Condition** column so the three cells
  holding non-enum decision strings resolve: "Allow with restrictions" and "Allow
  only in sandbox" become a guarded `ALLOW`, "Deny initially" becomes `DENY` in
  the `default` profile. **No outcome in the matrix changed.**
- Made the evaluator a **pure function** — `evaluate_deterministic(action,
  principal, run, ruleset)` — with time passed in as `evaluated_at` and never
  read from a clock, so the same inputs produce the same decision on replay.
- Established restrictiveness as a **total order combined by `max`**: `ALLOW` <
  `ALLOW_WITH_MODIFICATIONS` < `REQUIRE_APPROVAL` < `DENY`. Section 9.1's "more
  restrictive wins" was undefined across four decision types; it is now a
  computation. Hardline is not a rank in that order but a short-circuit.
- Located the hardline rules that Section 9.3 required but never placed:
  `src/agent_core/policy/hardline.yaml`, packaged, frozen at import, and
  deliberately **not** behind a port — a port implies substitution, and these
  are the rules that must not be substitutable. Every rule carries a mandatory
  `near_miss` it must permit, so an over-broad pattern fails its own test.
- Defined `policy_version`, which `ContextPlan` already consumed and nothing
  produced, as a **content hash** of the profile plus the hardline file
  (`default@3f2a1c9d4e5b+h7c1e0a92`), making rules version-controlled files
  frozen per process rather than rows in a table.
- Generalized approval beyond tool calls with `ActionKind` (tool call, memory
  write, skill authoring, artifact export), since `approvals.tool_invocation_id`
  being `NOT NULL` made the other three structurally unapprovable.
- Gave `approvals` the `tenant_id` and `principal_id` that Milestone 4's
  cross-tenant rejection criterion requires, three indexes, a unique index for
  one open approval per action, and a **guarded resolution** that is
  first-writer-wins: a second caller agreeing gets 200, a second caller
  disagreeing gets 409, and a cross-tenant caller gets not-found rather than
  forbidden.
- Specified the shape of "denial becomes a structured tool result" as a **field
  allowlist** enforced by test, with a repeated-denial circuit breaker at three,
  so a denial teaches the model to stop without teaching it what to evade.
- Mapped Section 22's three trust tiers onto Section 11.2's seven trust labels,
  with only `PLATFORM`, `TRUSTED_CONFIGURATION`, and `USER` able to authorize.
- Added `GET /v1/approvals` and `GET /v1/approvals/{id}`, without which Section
  17's `agent approval list` had no endpoint to call.
- Added ten hard gates, six tracked metrics, and a twelve-step build order to
  Milestone 4, keeping the advisory layer sequenced after Milestone 6.
- Wrote ADR-0006 as **already amended by ADR-0007**, so the distinction between
  "never persist reasoning" and "may hold provider-opaque continuation in the
  checkpoint for the life of a tool loop" lives in the record rather than in one
  trailing sentence of Section 11.4.
- No product implementation was performed.

## 2026-07-24 — Event log and persistence spec

- Added `docs/plan/event-log-and-persistence.md`, the persistence design for
  Sections 6.8, 6.9, 12.2, 14, and 15 and Milestone 2: the append transaction,
  projections, checkpoints, and the run queue. Recorded as ADR-0003 (amended for
  payload versioning) and ADR-0004.
- Stated the layer's contract as **observation, not durability** — a committed
  event no projection ever observed is, to every consumer, an event that did not
  happen — and wrote the hard gates against that definition.
- Identified a **silent missing-write hazard**: two appends take sequences 5 and
  6, the transaction holding 6 commits first, and a projection polling in that
  window advances past 5 and never sees it. The log stays consistent, `UNIQUE` is
  satisfied, and every rebuild reproduces the loss identically.
- Resolved it by establishing that Section 27.5's **one active run per session is
  load-bearing for projection correctness**, not only for contention, enforced by
  a partial unique index, with snapshot-aware watermarking
  (`pg_snapshot_xmin`) specified as the companion change required if that default
  is ever relaxed.
- Made **sequence gaps legal**: a rolled-back append burns its number, so
  consumers read after a watermark and never wait for a specific next sequence.
- Established `LISTEN`/`NOTIFY` as a **latency hint, never a delivery
  guarantee** — it is transactional, so no outbox is needed, and at-most-once, so
  every consumer polls from a watermark first.
- Added `events.payload_schema_version`, required by Section 6.8 and Milestone 2
  but absent from Section 15, together with **pure, total upcasters** that may
  never invent a value, and made an unknown higher version a hard error.
- Gave projections four properties — deterministic, watermarked, rebuildable,
  never authoritative — with state and cursor written in one transaction, and
  gave **derived events deterministic derivation keys** so rebuilds converge
  instead of multiplying their own output.
- Made checkpoints **deltas against periodic full snapshots** with the
  conversation stored as event references, and made *losing checkpoints costs
  time, not information* an executable test.
- Added claim **priority classes** (interactive, async, maintenance) with
  capacity reserved per class rather than aging, which would make latency depend
  on queue history.
- Made every worker write **fenced by `lease_epoch`**, since lease expiry is a
  guess: a zero-row update means stop, not retry, and `heartbeat` returns `False`
  when fenced rather than raising.
- Specified queue-level retry that the plan lacked entirely — only lease expiry
  requeues, `max_attempts` is 3, `runs.failure` is the dead letter — and added
  the `idempotency_keys` table that Section 16 and Milestone 2 both assume.
- Added seven hard gates and five tracked metrics to Milestone 2, four ports
  (`CheckpointRepository`, `ProjectionCursor`, `Projection`, `RunQueue`), and
  four event types.
- Created `docs/status/questions-for-review.md` recording every decision taken
  without review during the plan-completion run, with its reasoning, alternative,
  and reversal cost.
- No product implementation was performed.

## 2026-07-24 — Context engine spec

- Added `docs/plan/context-engine.md`, the assembly design for Section 11 and
  Milestone 7: the cache boundary, the budget allocator, compaction, trust
  rendering, and the working-state lifecycle. Recorded as ADR-0020.
- Split context into **two regions with one membership rule** — if a value *can*
  differ between two requests in the same session, it is not in the prefix.
  Membership is a property of item type, declared in code and asserted at assembly,
  so the current date cannot reach the prefix by looking like configuration.
- Made prompt stability **enforced rather than assumed**: `prefix_sha256` is
  recorded on every request, and a scripted fifty-turn session crossing midnight
  with a revoked tool, corrected memory, and a forced compaction must yield exactly
  one hash. Added **prefix epochs** for changes that cannot be absorbed, with
  epochs-per-session tracked against a target of 1.0.
- Pinned the tool set at session open and moved revocation to call-time policy
  denial, so a permission change does not rewrite the prefix or leak into cache
  timing.
- Gave `ContextBudget` a sizing rule: **only history scales with the context
  window**; every other class is capped absolutely, because prefix content is
  attention paid on every request. The prefix never yields — a class over its
  ceiling fails the session at open rather than truncating the system prompt.
- Fixed the yield order under pressure as in-turn recall, then tool-result
  truncation to typed pointers, then compaction, and made tool call/result pairs
  **atomic budget units**.
- Separated purity from compression: **`build()` is a pure function and compaction
  is a checkpoint write**, which is what makes retries safe and the byte-stability
  gate meaningful.
- Established that **untrusted content is elided, never paraphrased** — summarization
  is a trust-label laundering vector — with typed pointers retaining label, size,
  and reference, and a summary-depth cap of 2.
- Added the nonced trust envelope with delimiter escaping, and the typed
  `context.update_working_state` control tool with per-field carry rules across turn
  boundaries, bounded lists, and constraints that never evict.
- Handed `established_facts` to memory formation as candidates subject to every
  eligibility gate, giving the write path a second input rather than a bypass.
- Added five hard gates (determinism, prefix stability, budget conformance,
  tool-pair integrity, trust preservation) and four tracked metrics to Milestone 7.
- No product implementation was performed.

## 2026-07-24 — Session snapshot budget decided

- Closed the last retrieval open question. The session-open snapshot is capped by
  **item count first and tokens second**, never by a pure percentage of the context
  window: dilution tracks the absolute number of irrelevant items, so a larger window
  is not a reason for a larger snapshot. The percentage survives only as a ceiling.
- Set the starting `core` budgets: **40 items / 1,500 tokens** for interactive
  sessions, 80 / 3,000 for long-running async runs that amortize one block over many
  requests, and 15 / 500 for child runs — each bounded by 2% of the model's window.
- Reserved roughly two-thirds of the item budget for durable user-model and preference
  beliefs and the remainder for the opening-goal priming set, so project-specific
  beliefs cannot evict the "who am I talking to" layer the snapshot exists to carry.
- Made the number self-correcting rather than fixed: snapshot size should be
  **inversely proportional to retrieval quality** and is expected to shrink as the
  query former and ranker improve. Tuning is driven by two signals already present in
  `RecallTrace` — **snapshot utilization** (shrink below about a quarter) and
  **snapshot misses** (grow when in-turn recall keeps re-fetching snapshot-eligible
  beliefs) — which pull in opposite directions by design.
- Added the `Sizing the snapshot` section to the retrieval spec, recorded as ADR-0019
  decision 17, and rejected three alternatives: percentage-of-window sizing, one budget
  for every session type, and growing the snapshot as memory accumulates.
- Both memory specs now carry no open questions; the temporal entity graph remains
  unspecified.
- No product implementation was performed.

## 2026-07-24 — The recall trace becomes a user-inspectable surface

- Resolved the second retrieval open question: **the `RecallTrace` has two
  consumers** — the operator tuning ranking and the user asking why the agent said
  what it said — and both read the **same record**. Two logs would drift, and the
  one shown to the user is the one that must not be wrong.
- Specified that the trace is **recorded in the render pass, never reconstructed**,
  and bound to the exact rendered bytes by `rendered_sha256`. Re-running retrieval
  later returns a different set; a plausible reconstruction of a turn that never
  happened is worse than no answer.
- Defined what a trace may honestly claim: what was **in context**, with cited
  beliefs marked *used* and the rest *available*. It never claims what the model
  attended to.
- Added the user-safe projection (`RecallTraceView` / `TracedBelief`), which carries
  the statement, when and where it was learned, authority and source episode,
  confidence band, and citation, and excludes arm latencies, scores, candidate ids,
  and policy internals — dropped and blocked items are reported as counts only.
- Sensitivity is filtered by the **minimum of the recall surface's and the viewing
  surface's ceiling**, and retention is **two-tier over one record**: operator fields
  expire on the tuning window, user-safe fields live and die with their session.
- Added a `TraceStore` port, three failure modes (trace disagrees with what the model
  saw, trace as a disclosure path, a rejected belief returning after re-derivation),
  and two hard-gate evals: **trace faithfulness** and **correction durability**.
- Made rejection from a trace a **typed, first-class formation input**: not true
  (retire), was true and has changed (supersede), true elsewhere but not here (lower
  portability and record a negative scope override), and unspecified (flag and
  down-weight, never retire). Added `BeliefRejection` and the `MemoryStore` methods
  `reject` and `outstanding_rejections`.
- Established that **rejections are events that re-derivation replays**, matched by
  content rather than belief id since re-derivation mints new ids, and that
  **rejecting is not deleting** — a deletion keeps only a content-hash tombstone.
- Recorded as ADR-0019 decisions 15 and 16 and ADR-0018 decision 15, with build
  sequences updated in both specs so the trace is written faithfully from the first
  commit and rejections exist before re-derivation can violate them.
- No product implementation was performed.

## 2026-07-24 — Beliefs carry across projects

- Resolved the cross-project open question: **beliefs carry from project to project**
  so the agent learns from every project and environment it works in. Scope is split
  into **isolation boundaries** (tenant, principal, sensitivity — hard SQL predicates,
  unchanged) and **relevance boundaries** (project — a ranking and rendering input).
- Added a **portability** class per belief (`portable` / `contextual` / `local`),
  bounded by `belief_type` at formation and lowerable but never raisable by the
  extractor; carried beliefs render with their origin project and at a reduced
  confidence band, and explicit local overrides outrank them.
- Added **promotion by cross-project corroboration** to the formation spec: a belief
  independently observed in two or more project scopes promotes to `user` scope,
  retains every contributing origin, and emits `memory.promoted`. Recorded as
  ADR-0018 decision 14 and ADR-0019 decision 5.
- Added the **false transfer** failure mode with its defenses, and paired
  **transfer-precision / transfer-lift** evaluation metrics.
- Expanded the two remaining retrieval open questions with their tradeoffs: the
  session snapshot budget is attention-bound rather than cost-bound and should be an
  absolute token cap rather than a pure percentage; user-visible retrieval traces are
  restated in terms of the commitments they impose now (a user-safe projection,
  retention, the sensitivity ceiling, and a user-rejection input into formation).
- No product implementation was performed.

## 2026-07-24 — Memory retrieval & ranking spec

- Added `docs/plan/memory-retrieval-and-ranking.md`, the read-path design for
  long-term memory: the three retrieval moments forced by the prompt-stability
  invariant (frozen session snapshot, in-turn recall, child-run recall), query
  formation from working state, the hard scope filter, multi-arm recall fused by
  reciprocal rank, the explicit ranking function, supersession collapse, the safety
  pass, byte-stable rendering, retrieval traces, and the usage-feedback loop back
  into formation. Recorded as ADR-0019.
- Wired the spec and ADR-0019 into the MkDocs navigation and the ADR index, and
  added read-path pointers from Milestone 9 and the formation spec.
- Three open questions are recorded for decision: the session snapshot token
  budget, whether project-scoped beliefs may surface cross-project, and whether
  retrieval traces become a user-facing surface.
- No product implementation was performed.

## 2026-07-24 — Memory formation & consolidation spec

- Added `docs/plan/memory-formation-and-consolidation.md`, the detailed write-path
  design for long-term memory (formation pipeline, conflict resolution with
  bi-temporal supersession, data model, governance, evaluation, and build
  sequence). Recorded as ADR-0018.
- Wired the spec into the MkDocs navigation and the ADR index, and added a pointer from Milestone 9 in the engineering plan.
- Resolved two design decisions in the spec and ADR-0018: memory formation is **fully autonomous from the start** (safety via deterministic eligibility gates, the untrusted-content write ban, and after-the-fact review), and the **builtin consolidation path is built to parity before any external provider**.
- Resolved the remaining formation questions: a **tiered memory model** (a continuous confidence lifecycle plus an explicit working/episodic/semantic/archival hierarchy), the **user model is a projection** over user-scoped beliefs, and **re-derivation is opt-in** per principal.
- No product implementation was performed.

## 2026-07-20 — Documentation system established

- Archived the source Word document to
  `archive/Modular_General_Purpose_AI_Agent_Engineering_Plan.docx` and recorded
  its SHA-256 checksum in `archive/README.md`. Preserved the prior Word revisions
  (v1.0 through v2.3) under `archive/versions/`; the canonical archived document is
  a copy of v2.3.
- Converted the complete engineering plan to canonical Markdown at
  `docs/plan/engineering-plan.md` (Pandoc `docx` → `gfm`, then deterministic
  cleanup: single level-one title, fenced code blocks with language hints,
  normalized tables, and removal of the static Word table of contents and
  title-page artifacts).
- Relocated three security controls — non-bypassable hardline rules, tiered
  credential scrubbing with fail-closed passthrough, and default-deny pairing for
  untrusted inbound surfaces — from the "Revision summary" list to their correct
  home in Section 22, "Security baseline". No requirement text was changed; only
  placement was corrected. The archived `.docx` retains the original placement.
- Created coding-agent instruction files: `AGENTS.md`, `CLAUDE.md`, and
  `.github/copilot-instructions.md`.
- Created machine-readable project state at `docs/status/project-state.yaml`
  (current milestone: 0) and a concise `docs/plan/current-milestone.md`.
- Wired the existing `docs/adr/` records (ADR-0007 through ADR-0017) into the
  documentation site.
- Created the documentation build system: the MkDocs site (`mkdocs.yml`), a
  single-file HTML build (`docs-manifest.yaml`, `scripts/build_docs.py`),
  documentation validation (`scripts/check_docs.py`), `Makefile` targets, and a
  CI workflow (`.github/workflows/docs.yml`).

No product implementation was performed. Milestone 0 has not been started.
