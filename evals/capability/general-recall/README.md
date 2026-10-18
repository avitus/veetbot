# General recall development fixtures

The ten-case pilots are diagnostic development data. They do not count toward
the independent 400-scenario release corpus or permit activation. `pilot.v1.jsonl`
remains unchanged so its retained measurements can be reproduced.

`pilot.v2.jsonl` corrects development labeling before the paired comparisons:

- Explicitly authored equivalent subjects cover the observed project-decision,
  rail-travel, arrival-deadline and current-review-time paraphrases.
- The rationale answer aliases include the two observed salt-spray explanations.
  Those aliases change scoring only; they are never supplied to the reader.
- The workshop question requires an entrance claim. Merely retrieving the workshop
  schedule cannot certify that an answer naming Bay 6 used supporting evidence.

These changes require a complete new comparison. Earlier failures remain in their
original observations and are not selectively rescored into a passing run. The
owner approved the draft corpus on 2026-10-13 (`draft/review-manifest.json`);
the separate difficult-failure packet still needs human review.

The five `postgres-paired-v5-*` configurations compare sparse control, baseline
hybrid, reranking alone, recovery alone and their combination. They share one
formation bank per scenario, 1,000 distractors and the same context caps. Their
shared provider ceiling is USD 5, enforced by the matrix's single reservation
ledger across all arms. The earlier v4 split allowances refused the first worst-case
reservation before any provider send; its failed attempt remains recorded.
Actual formation is recorded once; subsequent
arms verify matching inputs and formed state before executing retrieval and their
real reader. The exploratory latency and case-cost ceilings are recorded separately
under `evals/observations/general-memory-recall/20261012-postgres-baselines/`.
