# ADR-0069: Milestone 16 memory evaluation and lifecycle

- Status: Proposed
- Date: 2026-08-22
- Related: Sections 20, 21, and 24 of the engineering plan; ADR-0014,
  ADR-0018, ADR-0019, ADR-0022, ADR-0045, ADR-0051, ADR-0057, ADR-0061
- Detailed design: `docs/plan/memory-evaluation-and-lifecycle.md`

## Context

The memory subsystem carries twenty-nine registered gates and every one of
them is a safety statement: it must not fabricate, must not resurrect a
rejection, must not cross a scope, must not render above a ceiling. None of
them says how well memory works. The two memory specifications name the
measurements that would answer that — consequential recall@k, noise ratio,
transfer precision and lift, end-to-end lift over multi-session scenarios,
formation precision and recall — and nothing in the repository computes any of
them. Every change to formation or ranking since Milestone 9 has therefore been
argued from reading a diff, and the plan's standing rule that a memory
capability enters on evaluation evidence has had nothing to read.

The same specifications describe a lifecycle the code does not have. Decay over
unused provisional beliefs, usage that resets decay without raising confidence,
the recall delta and its correction lines over the frozen snapshot, conflicts
surfaced instead of silently resolved, established facts as a formation input,
opt-in re-derivation, and the memory profile document itself are all designed
and none of them runs. `src/agent_core/memory/profiles.yaml` is loaded by
nothing.

The published survey work on agent memory frames the subject as forms,
functions, and dynamics — formation, evolution through consolidation, update,
and forgetting, and retrieval — and judges systems on long-horizon
multi-session benchmarks. Read against that frame, this platform has an unusual
shape: strong governance, no measurement, and a written but unbuilt evolution
half.

The owner authorized Milestone 16 on 2026-08-22 as a parallel workstream
alongside Milestones 12 through 15, rather than as a roadmap item, because the
missing yardstick blocks every remaining memory decision and the missing
lifecycle is already specified work.

## Proposed decisions

1. **Milestone 16 is authorized as a parallel workstream.** It may be developed
   alongside Milestones 12 through 15 exactly as Milestone 11 was developed
   alongside Milestone 10, and its gates may become green independently. The
   verified gate ceiling still advances only in numerical order, so nothing in
   this milestone moves the ceiling past 15. Roadmap item B6 narrows to the
   residue this milestone does not take: the semantic arm and `pgvector`, an
   external memory provider, the persona surface, a temporal entity graph,
   session history and artifacts as retrieval sources, and belief merge or
   global consolidation, each entering on Milestone 16 benchmark evidence for
   that item.
2. **Measurement precedes change.** The benchmark corpus, its metrics, and a
   checked-in baseline land before any lifecycle behavior moves, and the
   baseline is the yardstick every later change in the milestone is read
   against. Any behavior change re-records the baseline deliberately in the
   same change and justifies the delta in review; the exactness gate makes that
   mandatory rather than customary.
3. **No model judge anywhere in scoring.** Correctness is decided by
   checked-in normalized labels, token-bounded matching against gold values,
   and one exact abstention phrase. A judge would make the yardstick move on
   its own and would put a provider call inside the definition of "better".
4. **A CI-deterministic arm and an opt-in live arm with a fixed ceiling.** The
   deterministic arm runs in CI on the in-memory tier under a fixed clock,
   sequential identifiers, and a scripted model, and it scores formation and
   retrieval rather than answers. The live arm runs only under
   `RUN_LIVE_MODEL_TESTS=1`, pairs a with-memory and a without-memory arm over
   the same probes, and is bounded by a pre-admission ceiling of USD 4.00 per
   invocation. Its evidence artifact re-validates every pass condition from the
   counts inside it and is never overwritten.
5. **Public datasets are loaded from a local path and never vendored.**
   LongMemEval (MIT), LoCoMo (CC BY-NC 4.0), and HaluMem (CC BY-NC-ND 4.0) are
   read from a path the operator supplies; the repository holds no copy and no
   derivative; results are derived metrics naming the dataset, its license, the
   sample and seed, and the digest of the local file. The non-commercial and
   no-derivatives terms bind and are satisfied by local evaluation only. CI
   exercises the adapters against synthetic fixtures.
6. **Lexical retrieval is a ranking arm, not a hard filter.** Both store
   adapters adopt any-term semantics with a candidate cap and newest-first
   ordering, rather than the conjunctive PostgreSQL behavior on one side and no
   lexical predicate at all on the other. Conjunction turns a ranking arm into
   a filter and drops beliefs the ranker should merely demote; the divergence
   also meant the in-memory tier was not a real adapter.
7. **Three policy versions move, each once.** Deterministic formation goes
   `formation@5` to `formation@7` and provider-assisted formation `formation@6`
   to `formation@8` in the change that admits established facts (ADR-0068 had
   already moved them from `formation@2` and `formation@4`); retrieval goes
   `retrieval@1` to `retrieval@2` in the change that adds time decay and the
   near-duplicate penalty. The provider bump invalidates the bundled release
   evidence, so the milestone republishes it at `formation@8`, deletes the
   superseded artifact, and records the automatic-mode fallback and
   required-mode refusal in the release notes.
8. **Forgetting is decay over unused, provisional or low-confidence beliefs.**
   A bounded sweep lowers confidence on beliefs idle beyond a per-type
   half-life and retires those below a floor, guarded against decaying the same
   belief twice in a window. Usage resets decay and raises utility and never
   raises confidence, which restates the retrieval specification's standing
   decision: evidence must come from the world, not from the retriever.
9. **Unresolved conflicts are committed flagged and surfaced, never silently
   resolved.** An inference contradicting a higher-authority belief, or a
   contradiction between equally authoritative statements that nothing orders
   in time, commits flagged and linked in both directions, requests
   confirmation, and surfaces both statements in recall. Ordering is authority
   first, then recency; polarity alone never conflicts.
10. **The session idle boundary stays part of the formation policy, not a
    knob.** It is recorded in each belief's consolidation policy version, and a
    per-tenant idle threshold would make two beliefs formed under the same
    recorded policy incomparable. The interactive snapshot sizes move the other
    way, out of the memory profile document, because the context plan is
    already their authority.
11. **Re-derivation is an explicit operator action.** The command refuses
    without `--confirm`, re-consolidates from watermark zero under the current
    policy, and replays outstanding rejections so nothing a user rejected
    returns. Re-derivation remains opt-in per principal, as the formation
    design requires.
12. **One gate area, `memory`, nineteen gates.** The existing area takes them:
    the benchmark measures the same subject the area already covers, and its
    gates cross-reference formation and retrieval gates rather than standing
    apart from them. The `memory` area now spans three declaring
    specifications.

## Consequences

- The platform gains a number for memory quality. Nine benchmark gates and a
  checked-in baseline make an improvement and a regression distinguishable for
  the first time, and roadmap item B6's residue gains a way to earn entry.
- Every behavior change in this milestone and after it re-records the baseline
  in its own change, which makes the metric delta part of code review.
- A live evaluation costs money, is opt-in, and produces an artifact that is
  never overwritten; a re-record is a deliberate deletion.
- The provider-assisted extraction evidence is invalidated by the
  `formation@8` bump until it is republished; under the automatic mode the
  composition falls back to deterministic extraction with a content-free audit,
  and under the required mode startup refuses.
- Ten lifecycle gates land behavior the corpus has described since Milestone 9:
  decay, usage feedback, the recall delta, correction lines, established facts,
  conflicts, re-derivation, trace expiry, lexical parity, and the profile
  document.
- The shipped operator-reviewable knob inventory grows from 121 to 132 as the
  memory profile document becomes real, and three interactive snapshot knobs
  move to the context plan.
- One migration is added, for the trace operator-expiry index.

## Alternatives considered

- **Fold this work into Milestone 10:** rejected; Milestone 10's completion
  contract is fixed and already awaiting hosted review, and reopening it to add
  a benchmark would change a milestone that is done being changed.
- **Two milestones, measure then complete:** rejected; the lifecycle changes
  are the first real consumers of the benchmark, and splitting them would let
  the yardstick sit unexercised for a milestone and rot.
- **Capability breadth first — the semantic arm and the entity graph:**
  rejected; adding a recall arm with no way to measure lift is the exact
  decision the plan's own entry gate forbids, and the survey frame says the
  gap is evolution and evaluation rather than arms.
- **Queue this after Milestone 15:** rejected by the owner; the missing
  yardstick blocks memory decisions now, and the work shares no file with the
  notification, delegation, surface, or operations tranches.
- **Vendor the public datasets into the repository:** rejected; two of the
  three forbid it outright, all three are large, and a checked-in copy would
  make the licence terms a repository property rather than an operator one.
- **Score answers with an LLM judge, as the published LongMemEval numbers do:**
  rejected; it would make the baseline non-deterministic, put a provider call
  in CI or in the definition of correctness, and cost money on every run.
