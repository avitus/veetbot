# General recall: candidate selection and initial diagnostics

The category-independent candidate starvation repair is implemented. The broader
memory recall milestone is **not complete** and these files cannot enable semantic
retrieval, source expansion, or unattended dreaming. Reading lane: **A** (also the
diff-derived minimum).

## What changed

- Both stores rank permitted lexical and structured candidates independently
  before truncation, then deduplicate their union. Strong older facts survive
  newer weak matches across all five belief types. Exclusions precede limits.
- Automatic queries and `memory.search` select the new `retrieval@5` policy.
  `memory/profiles.yaml` exposes `retrieval.candidate_selection`; `recent` restores
  legacy candidate ordering. Saved queries default to that legacy selector.
- Frozen M16 and M32 drivers explicitly select legacy ordering. Their five
  manifest-bound corpora, baseline and scorer files remain byte-identical.
- ADR-0178 and `docs/plan/general-memory-recall.md` register the approved work as
  Milestone 33 with twelve pending gates. The gate census, routing, status,
  configuration count, documentation navigation and citation ledger are updated.
- New diagnostic/scoring modules and a 400-seed synthetic set measure actual
  automatic and deliberate recall, validate complete observation coverage and
  distinguish candidate misses from context-budget drops. Formation and answer
  success are unmeasured, represented as null, never synthesized by a fake reader.

Core changed files: `domain/memory_candidates.py`, both memory stores and the SQL
candidate helper, `memory/retrieval.py`, profiles, `tools/memory_search.py`, and
`bootstrap.py`. Diagnostic modules are `evals/general_memory_recall.py`,
`general_memory_recall_driver.py` and `general_memory_embeddings.py` under
`src/agent_core/`. Regression suites are `tests/contract/test_general_memory_recall.py`,
`tests/integration/test_general_memory_recall_postgres.py` and the two new unit
suites. Other production changes bind existing frozen drivers to their control.

## Measured limits

These are seeded, pooled-statement diagnostics: 400 synthetic information needs,
fifty per family. The pool supplies competing topics, plus the indicated newer
project-ticket distractors. All runs use 20 items and 2,000 context tokens.
The 100 and 1,000 load reports bind implementation-file hashes; the initial 10,000
load exploration preceded driver hash recording and is explicitly unbound.
None is an independent held-out, production-shaped or end-to-end release corpus.

| Newer distractors | Old automatic recall | New automatic recall |
| ---: | ---: | ---: |
| 100 | 68.75% | 69.00% |
| 1,000 | 67.00% | 68.75% |
| 10,000 (exploratory) | 67.00% | 68.75% |

At 1,000 distractors, deliberate recall also rises from 67% to 68.75%. Automatic
candidate recall rises from 68.25% to 70%; five target facts survive candidate
selection but are dropped for context budget. Preference recall remains 26%.
The mechanism repairs starvation; it cannot bridge missing semantic terms.
The original ownership compatibility branch remains until a general semantic
path passes its regressions. The diagnostic does not establish the approved 95%
recall target or the ten-percentage-point implicit-use target.

The embedding ablation fixes `text-embedding-3-small`, 256 dimensions, batches of
64, a 120-second request-loop deadline and a USD 0.05 ceiling before any send.
The price was checked against the official model documentation on 2026-10-11.
Response ordering, finite vectors, normalization, usage, pre-send reservation,
non-retry and response-text suppression have unit coverage. Initial attempts used the generic
credential and returned HTTP 401; all failed manifests retain their reservations.
The provider profile actually names `VEETBOT_OPENAI_KEY`. Correcting the experiment
with a failing credential-selection test enabled the real comparison:

| Top-20 candidate discovery | Recall |
| --- | ---: |
| Lexical/structured | 68.5% |
| Dense | 97.0% |
| Rank fusion | 96.5% |

All dense families reach at least 90%, with preference recall rising to 90%.
The run used 25,155 tokens and USD 0.00050310. These are candidate rankings before
rendering, not the earlier rendered-context score. The result justifies indexing
work but does not establish held-out implicit-use lift or production safety.
This offline helper is not a production embedding adapter or a budget certificate.

## Reproduction and verification

```sh
uv run python -m agent_core.evals.general_memory_recall_driver --noise 1000 --output test-results/memory/general-recall-1000.json
# Uses the repository provider credential; writes a new run, never overwrites one.
doppler run --project veetbot --config dev --no-fallback -- uv run python -m agent_core.evals.general_memory_embeddings --sparse test-results/memory/general-recall-1000.json --output test-results/memory/general-semantic-new.json --cost-ceiling-usd 0.05
```

`summary.json` records aggregate observations and SHA-256 digests. Compressed
JSON files preserve every diagnostic observation; decompress with Python's
standard `gzip` module. No real owner memory or conversations were exported.
The 100/1,000-record reports and semantic comparison were rerun after the historical
merge-support repair and retain the same quality scores. Earlier observations
remain in the `before-support-repair` files. The current semantic manifest binds
the exact decompressed `load-1000.json.gz` bytes it consumed.

The regression started red with
`uv run pytest tests/contract/test_general_memory_recall.py -q`: twelve failures
showed the older relevant memory excluded or lost despite its stronger match.
Automatic/deliberate wiring then failed two focused assertions before the policy
was wired. These were behavior failures, separate from sidecar setup failures.
The completed shared suite passes eighteen cases. Real PostgreSQL and mirror
parity passed all ten type/history combinations, including explicit exclusions,
principal, tenant, sensitivity and type restrictions; the existing PostgreSQL
memory partition also passed. Two additional SQL cases exercise independently
bounded candidate arms in current and historical queries. Frozen-control, query/ranking, profile/tool,
configuration and scorer tests were exercised. Final repository-wide verification
is required before this change is committed and reported to the owner.

The broad contract run exposed a historical-merge interaction: persona exclusions
must hide output candidates without preventing an internal, policy-filtered check
of original support. The existing `historical_scope_and_lookup_bound` regression
failed before the validation lookup was corrected. It now also asserts that the
excluded canonical record is not returned. All 55 focused recall contracts and
25 PostgreSQL candidate/history cases passed after the repair.

## Remaining work

The dedicated development credential works; no credential update is needed.
The next stages build the independent history/holdout/answer-use corpus and the
measured production index,
query planning, reranking and governed source recovery in the approved order.
Production index lifecycle, hard-boundary, latency/cost and answer-use gates remain
open. No original acceptance criterion was weakened; ADR-0178 explicitly extends
the earlier roadmap deferral. A PR and production delivery require the owner's
separate explicit instructions.
