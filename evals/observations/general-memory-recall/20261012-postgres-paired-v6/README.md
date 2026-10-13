# Task-context and reader-planning development comparison

Ten synthetic development scenarios, both recall paths, 1,000 newer distractors,
and normal `formation@11` ran against shared recorded formation responses and
verified formed state. These are task-context variants of the existing pilot,
not ten additional independent scenarios. No holdout case was executed.
The reader was `gpt-5.6-sol`; semantic arms used the verified local MiniLM tuple.
All arms retained the same twenty-item and two-thousand-token context limits.

| Arm | Automatic | Deliberate | Fresh provider USD | Reader p95 ms | Retrieval p95 ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Sparse | 3/10 | 1/10 | 0.45426425 | 2,468 | 319 |
| Dense | 1/10 | 1/10 | 0.21452625 | 5,160 | 120 |
| Hybrid | 4/10 | 1/10 | 0.21280500 | 3,911 | 351 |
| Relevance reranking | 9/10 | 1/10 | 0.21151750 | 4,136 | 405 |
| Reranking with task-context query | 9/10 | 1/10 | 0.21089625 | 3,342 | 489 |
| Reranking with reader-planned search | 10/10 | 6/10 | 0.33758625 | 5,857 | 593 |

The task-context ablation adds no answers in this sample. Automatic baseline
recall already includes the working objective. Scripted deliberate search uses
the current generic continuation request; reader-planned search can instead
form a useful query from the reader's actual working context. This is a
comparison of those mechanisms, not a claim that production always uses a
scripted query. The relationship case succeeds through both reader-planned paths.

All four reader-planned failures occur on the deliberate path: vehicle fuel,
dietary constraints, review cadence and entrance instructions. In each trace,
the model supplies hard `belief_types` that exclude the normally formed required
claims before candidate ranking. The vehicle and dietary facts are portable,
so the invented scope names in those queries do not explain their exclusion.
ADR-0198 repairs the model-facing type argument after this frozen experiment.
These observations cannot certify that repair.

The six arms completed sequentially in 4,868.694 seconds (81 minutes 9 seconds).
The shared, nonrenewable USD 5 ledger spent USD 1.64159550 on 168 fresh provider
calls and ended with no unsettled reservations. Only the first arm paid for
formation; `comparison.json` also reports attributed formation cost. Reader
elapsed time includes runtime retrieval, but excludes formation and fixture/index
setup. Quantiles use nearest rank. Dense and reader-planned search exceed the
exploratory 5,000 ms p95 ceiling; every arm remains below USD 0.12 per case and
the context ceiling. These are development ceilings, not release admission.

Paired intervals in `comparison.json` average both path outcomes per scenario
before treating the ten scenarios as independent units. The reader-planned arm
answers 16/20 versus sparse's 4/20; its paired normal 95% interval for the
60-point difference is approximately 31.5 to 88.5 percentage points. The sample
is small and does not establish held-out quality or release readiness.

The frozen implementation digest is recorded in each arm's `run.json`.
Subsequent changes include combined assembly-rank measurement, original-runtime
observation compilation and the model-facing type-hint repair. Original failures
are retained without selective rescoring. `banks/` contains the shared formation
records; `matrix/` contains the execution manifest and original provider ledger.
All histories are fabricated. Corpus independence and difficult-failure reviews,
the full release matrix, larger loads and matching final implementation evidence
remain outstanding. This directory cannot authorize optional-channel activation.
