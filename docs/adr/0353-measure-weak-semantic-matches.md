# ADR-0353: Measure how the indexed text affects weak semantic matches

- Status: Statement-only reranking rejected after the local comparison.
- Date: 2026-11-24
- Related: ADR-0188, ADR-0290, ADR-0346, ADR-0352

## Question

Four required facts reached the ranker but fell below the forty-item cutoff.
The local encoder reads a JSON array containing a subject and a statement.
A short statement might lose some of its meaning when the subject label and
serialization are included.

## Comparison

Replay the original encoder input and verify the vectors against the current
index. For the intervention, encode each statement alone with the same
verified local model. Rescore only the semantic hits that the existing index
returned for each read, using the original query vector. Break ties with the
existing seven-decimal ordering and original identifier. Pass the results to
the ordinary ranking and packing code. A separate diagnostic keeps the subject
and statement and removes the JSON formatting. Only the statement-only variant
affects the measured delivery.

The isolated probe database and its untouched template came from one approved
production copy. Both runs used the same development revision, scripted chat
model, scoring fingerprints, local encoder and budgets. The production index
and its admission were never edited. Candidate discovery outside the original
semantic shortlist and answer quality require separate measurements.

## Results and decision

Keep the existing source encoding. Re-encoding all 1,162 current indexed
originals reproduced every stored vector to a cosine similarity greater than
0.9999. All four required facts entered semantic search. Three shared zero
search terms with the question and the fourth shared one. The best eligible
holder of each fact ranked outside the top forty even before the final ranking
and packing steps.

| Original final rank | Best semantic rank across eligible holders | Best semantic rank with plain subject and statement | Best semantic rank with statement alone | Statement-only final rank |
| ---: | ---: | ---: | ---: | ---: |
| 56 | 54 | 48 | 76 | 87 |
| 72 | 52 | 59 | 44 | 52 |
| 136 | 125 | 123 | 145 | 136 |
| 164 | 139 | 92 | 77 | 84 |

The formatting and subject label affected relative order, most visibly for the
fact initially ranked 164th. Neither alternative brought any of the four facts
into the first forty semantic results. Across the four original queries, 309
of 375 higher-ranked semantic hits shared no more query terms than the
best-final-ranked holder. Exact word overlap alone cannot account for those
rankings. The limiting signal in this comparison was how closely the local
encoder matched the question to the stored fact. The experiment does not
identify which alternative model or representation would recover it.

| Same 114 questions | Before | Statement alone |
| --- | ---: | ---: |
| Development passed | 56/58 | 56/58 |
| Held-out passed | 53/56 | 53/56 |
| Required facts delivered per path | 120/125 | 120/125 |
| Forbidden deliveries per path | 0 | 0 |

Both paths delivered the same required facts in both runs. All 114 scoring
fingerprints matched the previous comparison. One held-out question absent
from the store came from the saved question file on the copy. Median automatic
recall was 122 ms before and 131 ms during the instrumented comparison.
Deliberate recall was 117 ms and 129 ms respectively. The disposable database,
derived vectors, temporary exports and credentials were deleted after the run.
