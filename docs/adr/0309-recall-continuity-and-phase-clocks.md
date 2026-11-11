# ADR-0309: Preserve automatic recall and time only admitted recall phases

- Status: Proposed under the owner's 2026-11-10 repair instruction
- Date: 2026-11-10
- Related: ADR-0183, ADR-0190, ADR-0195, ADR-0196, ADR-0282
- Detailed design: `docs/plan/general-memory-recall.md`

## Context

The owner requested repairs after an end-to-end production recall audit found
two failures. A full deliberate result displaced automatic evidence that its
narrower search could not recover. Separately, model thinking consumed the
durable recall deadline before a deliberate search could begin.

## Decision

For newly pinned `memory.search@2.3.0`, deliberate results may occupy at most
fifteen items and 1,500 tokens. Assembly reserves five items and 500 tokens for
fresh automatic recall within the unchanged shared twenty-item, 2,000-token
ceiling. The tool returns a bounded whole result with its original receipt;
assembly never edits evidence or invents receipts. The new receipt also names
the belief IDs actually rendered, checked against the authoritative trace. Fresh
automatic queries exclude only those validated, retained beliefs, so duplicates
do not consume the reserved capacity. Pre-render rankings never justify an
exclusion, and withheld results exclude nothing. Unused deliberate capacity
remains available to automatic recall. Older pinned tool versions retain their
selection behavior. Identity discovery keeps the original bounded candidate cap
and original hard exclusions independently of the presentation allocation and
already-rendered IDs. A smaller prompt must not turn ambiguous relationship
evidence into a single identity and thereby change the recall path. The new
search tool keeps the previous twenty-candidate identity-discovery ceiling.
Source validation, sensitivity ceilings, mandatory
corrections, frozen snapshots and erasure protections are unchanged. This is a
bounded allocation repair, not a global relevance rank or evidence cache.

New execution-bound reservations use a durable phase identity and a fifteen-second
deadline per admitted phase: at most five seconds for preparation and ten for
search. Model thinking and unrelated tool work occur outside those phases.
Automatic refreshes are bounded by the run's step limit; deliberate searches
retain the run-wide four-slot and two-deliberate-round ceilings, including the
automatic round's two slots. Each phase is checkpointed before work begins.
Re-entering a reserved phase cannot recreate a permit, including after resume
or checkpoint reconstruction. Old reservations without a phase identity keep
their original absolute deadline. Failed and cancelled work remains charged.

This explicitly amends ADR-0282's single run-wide recall deadline and
ADR-0195's deliberate-first allocation for the new tool version. The run's
deadline, cost and model-call limits still apply. No milestone, gate, semantic
activation or dreaming status changes.

## Verification

The regression sequence ran through CircleCI Smarter Testing in a local Linux
sandbox. The initial context-retention and twenty-second-thinking tests failed
for the observed behaviors before implementation (two failures, forty existing
passes). A later regression reproduced loss of a seventh-ranked fact when the
reserved slots were occupied by duplicates. The identity regression then
reproduced ambiguity collapsing under the smaller presentation window. These
were behavioral failures, not setup failures.

Focused commands use `circleci testsuite run static --run-tests=all -t` with
`tests/unit/test_context_memory_budget.py`,
`tests/unit/test_runtime_recall_preparation.py` and
`tests/unit/test_people_context.py` as the red-test atoms. Final coverage also
includes recall cache, deadline, delta, execution budget, tool version and owner
overview tests; contract coverage checks current-source validation and identical
preparation/execution queries for all three planned search versions. Serialized
phase replay cannot obtain a replacement permit, original hard exclusions remain
in identity discovery, and tampered returned-belief receipts fail validation.

A controlled replay on an isolated production database copy held the prompt and
search arguments fixed: version 2.2.0 delivered the missing fact only to the first
model call; version 2.3.0 delivered it to all three. Separate zero-delay and
twenty-second-delay probes both completed their deliberate search successfully,
without an automatic recall limitation. Production was not modified.

After integration with the current `dev` base, all 206 focused static tests and
106 contract tests passed in the Linux sandbox. Formatting, lint, type checking
and documentation checks passed. Reading lane A applies; no gate acceptance or
activation evidence is claimed by this diagnostic repair.

The earlier ADR-0309 audit cohort ran the fixed query corpus against an isolated copy of production release
`20261110-221916-d50bfe1`, using the revised code on `dev` base `9ef67a13`, the
unchanged standing semantic configuration and the production `astra/high` model
selection. It contained ten explicit and ten implicit questions, 34 expected
fact instances. All twenty runs completed. Actual model-visible text, including
verified equivalent records, carried 33/34 expected facts through the final
request. Answer review found 32 satisfied criteria, one omission and one
uncertain inference: eighteen tasks fully satisfied their memory criteria.
Both original explicit/implicit retention failures were resolved. The remaining
location miss never delivered the stored home-location fact; a separate response
received but did not explicitly use a swimming routine. These are query-planning
and answer-use follow-ups, not grounds to claim general recall accuracy improved.

There were no `memory.recall.limited` events. Two third deliberate searches were
refused after their checkpoints recorded four reserved slots and two deliberate
rounds, confirming the unchanged quota. All 1,964 stored memories retained their
content and non-usage state. This is a purposive single-observation diagnostic,
not held-out evaluation, independent human grading or milestone acceptance.
Private prompts, model receipts and source hashes stay outside repository docs.
The fixed corpus SHA-256 is
`dcc55c7240df68a7006d6699e11f30b3d383d932ad8990970ec803009c71e430`.
These are the original cohort's recorded counts, not the later ADR-0310 result.
The later audit restored a separate snapshot taken while the same release was
running, containing 1,974 memories, and completed 20/20 tasks with 34/34 facts
retained and used. See `docs/plan/general-memory-recall.md` for that final cohort.
The query-corpus digest identifies the questions, not the database snapshot.

The authorized repair changes the allocation and timing decisions described
above; it makes no further deviation from the plan. Production delivery remains
separate. Existing chats retain their pinned search version; a new chat after
delivery selects the new allocation policy.
