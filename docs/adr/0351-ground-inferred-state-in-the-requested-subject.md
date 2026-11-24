# ADR-0351: Ground an inferred state in the subject of the question

- Status: Proposed in a local recall experiment.
- Date: 2026-11-24
- Related: ADR-0248, ADR-0310, ADR-0346, ADR-0349
- Detailed design: [Recall selection](../plan/memory-retrieval-and-ranking.md#6-collapse-and-diversify).

## Finding

On a fresh copy, the owner question set reproduced 107 passes out of 114.
One held-out question received both a newer owner-confirmed answer at rank one
and an older provisional inference at rank five. Both the automatic and deliberate
paths delivered the pair. The records have different subject labels, types and
scopes, share a category noun and have no supersession or conflict link.
The weak record still met its visibility and validity checks. Moving the
confirmed answer higher could achieve nothing because it already ranked first.

## Decision under test

For a current relevance query that asks whether one qualified subject has a
state, check the subject of a standalone provisional state assertion. For
example, a question about a copper vessel being full does not establish that
a steel vessel's inventory answers it. Withhold the inference when all of
these conditions hold.

- The belief is an inferred, provisional recurring state.
- Its statement asserts or denies the requested state about its own stored
  subject in a simple present-tense clause.
- The two subject names end in the same noun, both have qualifiers, and none
  of their qualifying words match after case and plural normalization.

Apply the check to both recall paths before packing and again when validating a
retained search receipt. An automatic follow-up can exclude the confirmed fact
already in context. It must still check the subject of the remaining inference.

The rule changes presentation only. Subject labels alone cannot establish
whether two records refer to the same real-world object or justify retiring
one. The original remains available for a question about its own subject.
Preserve explicitly linked conflicts, owner-backed evidence, historical
queries, identity-scoped reads and explicit identifier reads. Broader questions
and qualified statements outside the supported grammar keep their existing
selection. Different aliases without shared qualifying words can lose a
provisional status answer under this rule.

## Evidence

The synthetic regression uses unrelated inventory subjects and both polarities,
including a second read with the confirmed fact already in context. Additional
checks cover retained receipts from older code, matching and unqualified
subjects, comparisons, independent predicates, and explicit conflicts.

Both scripted runs used the same starting database, the forty-item policy and
version-two scoring. The fresh copy supplied 113 active questions. The saved
held-out file supplied the missing question on the copy. All 114 scoring
fingerprints matched the preceding run, and semantic search ran on all 228 paths
before and after.

| Result | Before | After |
| --- | ---: | ---: |
| Development questions passed | 55 of 58 | 55 of 58 |
| Held-out questions passed | 52 of 56 | 53 of 56 |
| All questions passed | 107 of 114 | 108 of 114 |
| Forbidden deliveries on each path | 1 | 0 |
| Required facts delivered on each path | 119 of 125 | 119 of 125 |
| Median automatic recall time | 123 ms | 152 ms |
| Median deliberate recall time | 117 ms | 145 ms |

Every previously passing question and every previously delivered required fact
was preserved on both paths. Six questions still miss required evidence. The
timings are observations from the quality runs. Linux verification overlapped
the after-run. The copy was deleted, and no production result was recorded.
