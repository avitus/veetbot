# ADR-0193: Bounded task-context query planning

- Status: Experimental implementation under ADR-0178; activation remains gated
- Date: 2026-10-12
- Related: ADR-0178, ADR-0182, ADR-0190
- Detailed design: `docs/plan/general-memory-recall.md`

## Context

The baseline deterministic query combines the objective, open questions and current
message. A continuation such as "Continue" can omit the next action, unfinished
tasks and constraints already present in working state. This is an information-need
problem across memory categories, not a possession-specific recognition rule.

## Decision

Add a separately selectable `query_planning: task_context` experiment. Its default
is `baseline`, and current release admission is required on every call. Recent
candidate selection retains the frozen control even if the experiment is selected.
Withdrawal or unavailable admission restores the baseline immediately.

The deterministic composer starts with the complete current request, then considers
the next action, at most two unfinished tasks, two open questions, the objective,
two constraints and eight entity references from the first eight established facts.
It considers only the first eight tasks and excludes completed work. It deduplicates
whole fragments and retains at most 512 UTF-8 bytes. It never truncates a fragment.
A current request too large for enrichment takes the unchanged baseline path; the
embedding adapter still enforces its actual token limit and can withhold semantic
preparation. Byte bounding is not a claim about tokenizer equivalence.

The result is one information-need query. Scope and principal come from trusted
runtime arguments, never task prose. Dates and entities remain relevance hints;
the planner does not invent an `as_of`, `known_at` or mandatory belief filter.
Working state is not mutated and no completed answer or source evidence is cached.
The same former instance supplies preparation and actual context retrieval, so
their query fingerprints remain identical. Existing run reservations, cancellation,
context caps and source visibility still apply. No new model call is added.

## Evidence and limits

The continuation regression first omitted the next action and pending work, then
passed with the experimental composer. Admission withdrawal, unavailable evidence,
completed-task exclusion, whole-fragment bounds, long-request preservation and
matching preparation/recall queries have regression coverage. The profile and
isolated evaluator expose the strategy for a separate ablation.

These tests do not establish answer lift, an optimal fragment order, complete
shared output budgeting or an assisted second-round strategy. Production activation
and those remaining M33 gates still require the reviewed comparisons. No frozen
M16/M32 artifact or default retrieval behavior is changed.
