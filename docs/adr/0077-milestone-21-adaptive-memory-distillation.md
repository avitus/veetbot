# ADR-0077: Milestone 21 adaptive memory distillation

- Status: Proposed
- Date: 2026-08-31
- Related: Sections 13, 20, 21, 22, and 24 of the engineering plan;
  ADR-0014, ADR-0018, ADR-0019, ADR-0022, ADR-0045, ADR-0051, ADR-0057,
  ADR-0068, ADR-0069, ADR-0070
- Detailed design: `docs/plan/adaptive-memory-distillation.md`

## Context

The production memory former is too timid to serve as personal-agent memory.
The statement "I am building a personal AI agent" does not produce the direct
ongoing-project memory it plainly supports, and the system has no principled
way to retain a useful weaker inference such as likely software-development
experience. The current provider claim vocabulary is narrow, the automatic
candidate ceiling is twelve, and the provider is instructed not to extrapolate.
At the same time, usage feedback moves `last_reinforced_at`, so a recalled claim
can extend the clock that forgetting treats as evidence.

The owner explicitly accepts some provisional false positives in return for a
marked recall improvement, provided unsupported hypotheses and ongoing states
fade when later evidence does not refresh them. This is a new parallel
workstream even though the previous project-state file ended at Milestone 20.
The authorization is the input for this ADR; the state, map, readiness review,
and gates are updated before implementation begins.

Nemori supplies a useful inference-time shape: integrate episodes, anticipate
from the causal prefix and existing memory, and distill prediction error.
Mem-α supplies the downstream-usefulness objective. Neither justifies importing a
training loop, reinforcement learning, or a new dependency into this milestone.

## Proposed decisions

1. **Adaptive memory distillation is parallel Milestone 21.** It does not move
   the sequential verified ceiling past Milestone 12 or reorder Milestones 13
   through 15.
2. **Recall is the primary optimization target.** Direct useful recall, useful
   hypothesis recall, and benign precision are gated explicitly. The design
   tolerates some provisional false positives instead of optimizing for an
   almost-empty store.
3. **Completed controls remain frozen.** `formation@7` and `formation@8` keep
   their exact historical semantics and evidence. The new policy is
   `formation@9`, extractor `nemori-assisted-v1`.
4. **One consolidation makes three fixed batched calls.** Episode integration,
   causally blinded anticipation, and prediction-error distillation each run
   once. Candidate count never increases provider-call count.
5. **Integrated episodes are persisted derived records.** They remain fully
   attributable to ordered user events, are idempotent and rebuildable, and
   participate in session and principal erasure.
6. **Direct observations and hypotheses are first-class.** Candidates and
   beliefs carry a closed claim kind, `direct|hypothesis` derivation,
   `ongoing|durable|tentative` longevity, and exact evidence spans. Local code
   owns canonical rendering and uncertainty language.
7. **Capacity rises to thirty-two candidates per consolidation and six per
   source event.** Direct claims, future usefulness, and subject/category
   diversity determine the bounded order. Every displaced proposal is audited.
8. **Predictability is attributable.** A provider prediction suppresses a
   claim only when it cites a live memory that already represents the claim.
   General model expectation cannot suppress a directly stated fact.
9. **Evidence and usage clocks separate.** `last_evidence_at` is refreshed only
   by supporting user evidence or an authoritative human correction.
   `last_used_at` and utility record faithful citation. Citation does not extend
   evidence freshness, confidence, or expiry.
10. **Forgetting follows derivation and longevity.** Tentative hypotheses expire
    after thirty unsupported days, ongoing observations after ninety, durable
    direct beliefs on type-aware horizons, and explicit remembered beliefs do
    not decay automatically.
11. **Retrieval becomes `retrieval@3`; lifecycle becomes `lifecycle@2`.** Recall
    ranks against evidence age and visibly renders derivation and longevity on
    governed context, trace, CLI, and API surfaces.
12. **Trust exclusions stay narrow.** Invalid or foreign provenance,
    assistant-as-user attribution, credentials, promoted untrusted
    instructions, tenant/principal crossing, and durable human corrections are
    refused. Inference, ambiguity, ongoing state, or sensitivity alone is not a
    refusal reason.
13. **Activation requires comparative evidence.** A corpus of at least sixty
    cases compares `formation@7`, `formation@8`, and `formation@9` and must show
    the declared direct, hypothesis, precision, useful-lift, lifecycle, and
    zero-boundary-failure thresholds on the exact production tuple.
14. **Twenty-four gates own the workstream.** They are registered before the
    first production-code change and cover integration, blinding, fixed call
    count, fallback, recall, candidate bounds, grounding, telemetry, evidence
    clocks, forgetting, promotion, rendering, corrections, persistence,
    evaluation, and activation.

## Consequences

- Ordinary projects, goals, constraints, skills, and recurring states become
  formable memory rather than extractor blind spots.
- Useful inferences can help future turns while remaining visibly tentative and
  short-lived unless conversation corroborates them.
- A cited but wrong memory no longer perpetuates its own evidence lifetime.
- Three provider calls add background latency and cost per consolidation, but
  batching prevents that cost from scaling with the number of candidates.
- The belief schema and every human inspection projection grow. A structural
  migration, bounded backfill, and shared repository contracts are required.
- Provider assistance remains evidence-gated. Until a valid `formation@9`
  artifact exists, automatic composition keeps the currently evidenced
  `formation@8` rather than treating the new code as activated.
- The deterministic and previous provider policies remain available as honest
  controls, so a claimed lift can be reproduced rather than inferred from two
  unrelated runs.

## Alternatives considered

- **Only widen the regex extractor:** rejected as the whole solution. It can
  patch the motivating sentence but cannot coherently integrate episodes,
  distinguish what memory predicted, or generate useful bounded hypotheses.
  A high-recall deterministic fallback is still included.
- **Remove all formation filters:** rejected. Invalid provenance, credentials,
  promoted injection, corrections, and isolation are trust boundaries rather
  than precision preferences.
- **Keep hypotheses out of durable storage:** rejected. They are useful exactly
  because they can inform later turns; explicit derivation, low confidence,
  evidence-based expiry, and correction make storage governable.
- **Let citations refresh the evidence clock:** rejected. It creates a
  self-confirming loop in which retrieval is mistaken for corroboration.
- **Use one provider call:** rejected. Seeing the evidence while predicting it
  destroys the prediction-error signal, while combining integration and
  distillation prevents the integrated episode from becoming an inspectable,
  independently grounded artifact.
- **Train a memory policy with reinforcement learning:** deferred. The current
  repository has no training system and no need for one to test the inference-
  time architecture and usefulness objective.
- **Add vector retrieval or an external memory service at the same time:**
  rejected. Formation recall and forgetting can be measured on the existing
  retrieval substrate; adding another retrieval variable would make the source
  of any lift ambiguous.
