# ADR-0352: Scale a multi-word recall match to the available evidence

- Status: Proposed for local measurement.
- Date: 2026-11-24
- Related: ADR-0191, ADR-0346, ADR-0349, ADR-0351

## Finding

A belief can rank highly among word matches yet receive little credit in hybrid
recall. The lexical score divides matched terms by every content term in the
question. Even the strongest permitted match can cover only a fraction of a
long request. This mechanism can affect questions with more than one fact to
retrieve. It cannot supply words shared by neither the question nor the belief.

## Decision under test

Within the admitted hybrid relevance reranker, find the largest number of
distinct query terms matched by a remaining candidate after equivalence
collapse. For a belief matching at least two terms, divide its count by that
maximum. A lone term keeps its original fraction of the full query. Keep the
existing semantic and structured signals, lifecycle weights, eligibility
rules, packing limits and admission checks. Record the additional policy
component on the trace and rendered block when the denominator changes.

Compare both recall paths on the same restored snapshot. Preserve each
question's scoring fingerprint. Measure gains and losses separately on the
development and held-out halves and check forbidden deliveries.

## Measurement

Five required facts made the ranked list below the forty-item cutoff. The
best-ranked holder of each fact placed between 54th and 139th in semantic
search. Three had zero matching query terms, one had a single match shared
with many other candidates, and none of the five had a structured match.

The remaining fact matched two of thirteen query terms. The best candidate
matched four. The fact ranked eleventh in word search and seventy-first in
semantic search. With a denominator of four it moved from forty-third to
eighth overall and reached the reader on both paths. The four other facts
kept their original ranks.

| Same 114 scoring fingerprints, report version 2 | Before | After |
| --- | ---: | ---: |
| Development questions passed | 55/58 | 56/58 |
| Held-out questions passed | 53/56 | 53/56 |
| All questions passed | 108/114 | 109/114 |
| Required facts delivered on each path | 119/125 | 120/125 |
| Forbidden deliveries on each path | 0 | 0 |

Every previously passing question and previously delivered required fact was
preserved. The single gain was on the development set. Both runs restored the
same initial state, including the saved held-out question absent from the
production store. Semantic search ran on all 228 paths in each run. The local
median recall times were 123 and 145 ms for automatic recall, and 124 and
142 ms for deliberate recall, before and after respectively. Verification
work overlapped these timings. The production copy was deleted. These
scripted runs measure evidence delivery. Release activation and the broader
recall milestone have separate gates.
