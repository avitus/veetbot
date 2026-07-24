# ADR-0018: Memory formation and consolidation

- Status: Proposed
- Date: 2026-07-21
- Related: Milestone 9 (long-term memory), ADR-0006/0007 (no raw reasoning storage),
  ADR-0014 (memory surface and external providers), Section 6.8 (event log),
  Section 20 (evaluation), Section 27.6 (child runs), Section 30 (self-improving skills)
- Detailed design: `docs/plan/memory-formation-and-consolidation.md`

## Context

Long-term memory quality is won on the **write path** — deciding what is worth
remembering, in what form, and how it reconciles with existing beliefs — not on
retrieval. Naive designs extract facts every turn and embed them, which is noisy,
drifts, cannot reconcile contradictions, and is vulnerable to memory-poisoning via
prompt injection. Because this platform already has an append-only episodic event
log as source of truth, long-term memory can be a governed, re-derivable
projection over that log rather than a lossy side-effect write.

## Decision

1. **Memory is a governed projection over the episodic log.** Beliefs are derived
   from events, carry provenance, and can be **re-derived** ("re-remember") when
   the consolidation model or prompt improves. Every belief records the
   `consolidation_policy_version` that produced it.
2. **Formation is a deliberate, gated pipeline**, not an implicit effect of
   reading content: trigger → segment/select → extract candidates → filter
   (eligibility + salience) → resolve (dedupe/reinforce/supersede) → gate (policy +
   safety) → commit → decay/re-derive.
3. **Untrusted content can never directly form memory.** `EXTERNAL_UNTRUSTED`
   spans are evidence requiring user or agent affirmation, never a formation
   source — the primary injection defense on the write path.
4. **Precision over recall**, with a provisional tier that promotes only on
   reinforcement.
5. **Contradictions are never silently overwritten.** New beliefs supersede old
   ones with **bi-temporal validity** (`valid_to`, `superseded_by`); resolution is by source authority then recency. Ambiguous or sensitive resolutions are committed autonomously and flagged for review, never held for confirmation.
6. **Consolidation runs as a restricted child run / background job** (limited tool
   set, own budget, sandboxed), on a tiered cadence (cheap turn-boundary flag;
   session-boundary as primary; explicit; scheduled), never blocking the
   interactive turn.
7. **Everything is transparent and reversible**: provenance to source events and
   formation run; user inspect/edit/delete; memory changes emit events.
8. **Formation is gated by evaluation** (precision, consequential recall,
   contradiction handling, no-fabrication, injection resistance, cost).
9. **Formation is fully autonomous from the start.** No belief requires synchronous human confirmation; safety rests on the deterministic eligibility gates, the untrusted-content write ban, and after-the-fact transparency and reversibility.
10. **The builtin consolidation path is built to parity before an external provider is introduced** (an external provider is a later comparison option behind the `MemoryConsolidator` port).
11. **Memory is tiered on two axes**: a continuous confidence lifecycle (provisional -> active -> retired - thresholds over a score, not many discrete tiers) and an explicit memory hierarchy (working -> episodic -> semantic -> archival) that formation promotes across.
12. **The user model is a projection over user-scoped beliefs**, not a separate artifact.
13. **Re-derivation is opt-in per principal**, not automatic on consolidation-policy upgrades.
14. **Belief matching is scoped to the principal, not the project, and cross-project corroboration promotes.** A belief independently corroborated in two or more distinct project scopes is promoted to `user` scope, retaining every contributing origin in provenance and emitting `memory.promoted`. Each `belief_type` carries a default **portability** class (`portable` / `contextual` / `local`) that the extractor may lower but never raise; `local` beliefs never promote. This is the write-path half of the ADR-0019 decision to let beliefs carry across projects, so the agent learns from every project and environment it works in.
15. **User rejections are typed formation input that every run, including re-derivation, must replay.** A correction arriving from a recall trace (ADR-0019) is one of: not true (retire — `valid_to = now`, no replacement), was true and has changed (supersede with the correction as a new belief at user authority), or true elsewhere but not here (lower `portability` and record a negative scope override). An unspecified rejection flags for review and down-weights; it never retires on an ambiguous signal. A rejection is evidence at the highest authority and enters through the ordinary conflict resolver, not a special case. It is stored as a durable `BeliefRejection` event and applied **before commit on every consolidation run**, because re-derivation re-forms beliefs from the same episodes and would otherwise resurrect everything the user has ever corrected. Matching is by **content** (subject, statement, `belief_type`), not by belief id, since re-derivation mints new ids. **Rejecting is not deleting**: a correction retains its content for audit and replay, while a deletion removes the record and keeps only a `statement_sha256` tombstone, so re-derivation can refuse to re-form a belief without the platform continuing to hold what the user asked it to forget.

## Consequences

- A memory layer that accretes a genuine, auditable, correctable model of the user
  and their world — the "excel at long-term memory building" goal — rather than a
  vector dump.
- Re-derivation turns prompt/model improvements into better memory over the whole
  history, a capability most memory stacks cannot offer.
- Added machinery: a consolidation pipeline, `ConsolidationRun` records, extended
  `MemoryRecord` fields, and `MemoryConsolidator` / `Salience` / `ConflictResolver`
  ports. Consolidation has a real (if cheap-tier) token cost.
- Bi-temporal beliefs and conflict edges add query and storage complexity, paid
  back in correctness on changing facts.
- Formation gains a second input alongside the episodic log: outstanding rejections
  are consulted on every run, so re-derivation is safe to offer rather than a
  standing threat to undo the user's corrections.
- Corrections become a measurable formation-quality signal (rejection rate) drawn
  from real usage rather than a rubric — the cheapest evaluation in the memory layer,
  and the one that arrives without being asked for.
- Content-keyed matching means the rejection set must be checked against candidates
  by similarity rather than by key lookup, adding work to the resolve stage
  proportional to the number of outstanding rejections for the principal.
- Deletion and correction stop being the same operation, which requires the surface
  to distinguish them and the store to keep a hash-only tombstone for one of them.

## Alternatives considered

- **Per-turn automatic extraction + embed (naive RAG memory)**: rejected; noisy,
  drifts, no reconciliation, injection-prone.
- **Explicit-write only (no consolidation)**: high precision but misses most of
  what makes memory feel intelligent; kept as build-sequence step 1, not the end
  state.
- **Overwrite-on-contradiction**: rejected; loses history and truthfully-past
  beliefs, and makes errors unrecoverable.
- **Delegate entirely to an external memory provider**: viable behind the
  `MemoryConsolidator` port (ADR-0014), but the governance, provenance, and
  re-derivation guarantees stay ours; not adopted as the sole path.
- **Proliferating discrete confidence tiers**: rejected; confidence is continuous, so thresholds over a score give the same control without classification burden or schema churn.
- **Per-project belief stores**: rejected; identical beliefs would be formed and maintained independently per project, supersession would have to be applied N times, and the cross-project corroboration signal that identifies a genuinely general belief would be destroyed by the duplication.
- **Applying user corrections as edits to the belief store**: rejected; an edit is
  output, and re-derivation regenerates output from the log. Every correction would be
  silently reverted by the next policy upgrade — an invisible, delayed failure that is
  indistinguishable from the agent ignoring the user.
- **Matching rejections by belief id**: rejected; re-derivation mints new ids for the
  same content, so an id-keyed rejection stops matching precisely when it matters most.
- **Treating "this is wrong" as a delete**: rejected; discarding the content discards
  the evidence needed to refuse re-formation, and conflates a correction the user wants
  remembered with data they want removed.
