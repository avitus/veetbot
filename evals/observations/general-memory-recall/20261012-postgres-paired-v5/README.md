# Paired PostgreSQL development comparison

Ten synthetic development scenarios, both recall paths, 1,000 newer distractors,
and normal `formation@11` ran against the same recorded formation completions and
verified formed state in every arm. The reader was `gpt-5.6-sol`; semantic arms
used the verified local MiniLM tuple. Every run retained the same 20-item and
2,000-token output limits. Deliberate queries were scripted, so this experiment
does not measure assisted planning. No holdout case was executed.

| Arm | Correct answers | Fresh provider USD | Reader p95 ms | Retrieval p95 ms |
| --- | ---: | ---: | ---: | ---: |
| Sparse control | 8/20 | 0.46941325 | 3,179 | 344 |
| Hybrid baseline | 6/20 | 0.18783000 | 3,751 | 420 |
| Hybrid with relevance reranking | 18/20 | 0.18521875 | 3,409 | 352 |
| Hybrid with source recovery | 6/20 | 0.22682750 | 3,958 | 401 |
| Hybrid with both | 18/20 | 0.21616750 | 3,433 | 709 |

All five arms completed in 3,873.019 seconds, sequentially, with no overlapping
validation jobs. The shared, nonrenewable USD 5 ledger spent USD 1.285457 across
136 fresh provider calls. Only the first arm paid for formation; the others replayed
those exact completions. `comparison.json` reports attributed formation cost
separately, rather than implying that the later arms inherently cost less.
Reader elapsed time includes runtime context and search work but excludes formation
and fixture/index setup, which each observation records separately. Quantiles use
the nearest-rank definition. All arms met the exploratory 5,000 ms and USD 0.12
per-case ceilings; these are not confirmatory release budgets.

The reranking gain is paired on scenario and path. Both paths agreed within each
scenario, so the confidence calculation averages paths before treating the ten
scenarios as independent units. The 50-point gain over sparse has a paired normal
95% interval of approximately 17.3 to 82.7 percentage points. This small,
development-only comparison motivates further evaluation, not a population claim.
Source recovery added quotes and cost but no answer gain in this sample. The
baseline fusion lost the door-detail case that sparse answered, so semantic
retrieval alone is not a passing configuration.

The relationship scenario failed in every arm. Recorded formation proposed both
the sibling and spouse claims, but the implementation discarded the named spouse
statement and merged separate claims under one subject. The subsequent formation
repair is outside this experiment's frozen implementation digest. Later context
budget and retained-evidence validation repairs are also outside that digest.
Nothing in this directory certifies those later changes.

Each arm retains its original corpus, run identity and observations. `banks/`
contains the normalized structured formation responses and their state digests;
`matrix/` retains the shared cost journal and complete execution manifest.
These artifacts contain fabricated histories. This experiment cannot provide a
release receipt: corpus independence and difficult-failure review, the full matrix,
combined rank accounting, larger loads and final implementation evidence remain
unverified. Automatic admission remains disabled.

## Original records

[The verified archive](original-records.tar.gz) preserves all 28 original data
files byte for byte, with their repository-relative paths. This packaging keeps
the combined release within the hosted review's file limit; no observation,
answer, run identity, cost journal or formation bank was changed or omitted.

Archive SHA-256: `cc98522d66fb3d3f9bbf3fa110d8b5eb4b88a603fa1c1be4eb426e8e19e48326`.
The archive includes `original-file-sha256.json` with every original path, byte
count and SHA-256 digest. Each extracted file was compared with its committed
original before the loose copies were removed. Extract into a separate directory
to inspect the original layout. The original build references and experimental
limitations above remain unchanged; packaging supplies no new release evidence.
