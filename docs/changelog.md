---
title: Changelog
---

# Changelog

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
