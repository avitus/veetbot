# Capability scenarios

This directory is the live, non-deterministic evaluation track. The harness
defaults to five repeats, persists each repeat and each criterion score, and
keeps scenario, suite, and daily cost ceilings separate from score.

A scenario is admitted only when its `source` points to a checked-in, redacted
trajectory whose `export_id`, `run_id`, and failed outcome match the scenario.
Synthetic failures and hand-written anecdotes are not publishable scenarios.
This repository intentionally has no scenario yet because no actual failed,
redacted trajectory is present. Add the first real export under
`fixtures/trajectories/`, diagnose it, and then add its scenario under
`scenarios/`.

Run the track with:

```text
RUN_LIVE_MODEL_TESTS=1 uv run agent eval capability --suite research
```

Without the live flag, the command skips without loading credentials or making
provider calls.

`provider-memory-evidence.example.json` is a schema example, not activation
evidence. Generate a real artifact with the checked-in 25-case labeled corpus:

```text
RUN_LIVE_MODEL_TESTS=1 uv run agent eval memory-formation \
  --model-policy balanced --policy-profile default \
  --build-ref <immutable-build-ref> --output <new-evidence-path>
```

The command refuses to overwrite an existing path. It resolves the provider and
model, computes the corpus hash, compares isolated deterministic and provider
arms, and atomically creates the artifact only after the typed activation gate
passes. Passing requires lift over the deterministic arm, complete support for at
least 17 of the 21 positive cases, zero fabrication in both arms, and no policy
regression. Every run prints structured per-case arm diagnostics; a failed run
exits non-zero and prints those diagnostics without creating an activation
artifact. The 25 provider calls have a combined USD 1.25 policy ceiling. Startup
still checks the artifact against the exact active extraction tuple.

## Memory benchmark

`memory-benchmark.v1.json` is the multi-session memory benchmark corpus:
sixteen scenarios of conversational sessions, the beliefs those sessions state,
and the probes asked afterwards. It measures what memory forms across sessions,
what recall returns when a later probe asks, and what must never render —
formation precision and recall, end-to-end and given-formed recall, noise ratio,
and the counts for protected content, superseded statements, and cross-project
transfer. Roughly seventy per cent of the labels form under the deterministic
extractor; the rest are the extractor's headroom, reported and never gated.

The deterministic arm drives real formation and real retrieval through the
composition root on the in-memory tier, under a fixed clock, sequential
identifiers, and a scripted model, so it never reaches a network:

```text
uv run agent eval memory-benchmark --deterministic-only
```

`memory-benchmark.baseline.json` is that arm's recorded run. It is compared
exactly, not within a tolerance: `gate.memory.bench_no_regression` fails on
drift or on any regression, and `gate.memory.bench_baseline_current` fails on
any difference at all. A change that moves a single count therefore re-records
the baseline in the same change, where the delta is visible in review:

```text
git rm evals/capability/memory-benchmark.baseline.json
uv run agent eval memory-benchmark --deterministic-only \
  --write-baseline evals/capability/memory-benchmark.baseline.json \
  --build-ref <immutable-build-ref>
```

The write refuses an existing path, which is why re-recording removes the old
file first. Never record a baseline while a structural zero is violated:
formation policy failures, abstention leaks, currency violations, incomplete
probe runs, or more than one prompt prefix per probe.

### The live arm

The live arm asks every probe twice against a real model — once against the
composition the scenario's own conversations built, and once against a
composition that never saw them — and the difference between the two arms is
the number that justifies the subsystem. It is opt-in and it publishes its
evidence to a path that must not already exist:

```text
RUN_LIVE_MODEL_TESTS=1 uv run agent eval memory-benchmark \
  --no-deterministic-only --model-policy balanced --policy-profile default \
  --build-ref <immutable-build-ref> \
  --output evals/capability/memory-benchmark-evidence.<build-ref>.json
```

`--output` is required for a live run and refused for a deterministic one,
which records a baseline instead; `--build-ref` is required for a live run and
is never guessed from the working tree. Without the opt-in the command skips
cleanly and makes no provider call. Store an artifact under
`evals/capability/` at a fresh path per run: evidence is never overwritten, and
two runs are two documents.

The invocation costs at most **USD 4.00**, and the ceiling is enforced before
admission rather than after the fact: before each run the harness adds the
per-run ceiling of USD 0.05 to what it has spent, and stops with
`stopped_by="cost_ceiling"` rather than admitting the run that would cross it.
A model whose catalog price is zero would make that arithmetic vacuous, so the
first completed run reporting a cost of zero aborts the arm with "model pricing
unavailable; ceiling unenforceable".

A probe arm whose run ends without an answer is retried exactly once against a
freshly built composition, admitted through that same pre-admission check, and
a second failure is kept: the command prints the terminal status, error class,
model calls, and cost of every incomplete run beside its one-line summary, the
artifact carries `retried_runs` and a `failure_classes` histogram, and the
condition that no run may be left incomplete is unchanged.

The published artifact asserts, and re-checks in its own validator at parse
time, that: the lift over the without-memory arm reached twenty per cent of the
answerable probes; the with-memory arm answered at least eighty per cent of the
probes whose needed beliefs it actually recalled; it abstained on at least
eighty per cent of the probes that must abstain; no answer leaked protected
content; the with-memory arm's policy failures did not exceed the
without-memory arm's; no run was left incomplete; the ceiling was never hit;
the spend stayed under it; and one provider, model, and compiled policy version
covered every run. The thresholds are derived from the counts inside the
artifact rather than written into it, so an artifact that exists is an artifact
that passed, and a failing run prints its diagnostics and writes nothing.

Two artifacts are comparable only when they share the corpus digest, the
benchmark version, the formation and retrieval policy versions, and the
provider, model, and policy tuple — all of which the artifact carries. Live
answers vary run to run, so a single artifact is evidence that the gate passed
once, not a measurement to compare against a later one at the digit; the
deterministic baseline is the arm that is compared exactly.

### External benchmarks

Three public long-horizon benchmarks are the outside check on the corpus:
LongMemEval (MIT), LoCoMo (CC BY-NC 4.0), and HaluMem (CC BY-NC-ND 4.0). All
three are **downloaded by the operator and read from a local path**. Their
non-commercial and no-derivatives terms bind, and they are kept the same way:

- **no dataset file, and no derivative of one, is ever committed here.** The
  typical file names are in `.gitignore` under "External evaluation data";
  `tests/fixtures/memory_benchmark_external/` holds tiny synthetic fixtures that
  are invented, shaped like each dataset, and named so they do not match those
  patterns. CI exercises the adapters against those fixtures only.
- what leaves a run is a metrics document naming the dataset, its license, the
  sample size and seed, and the `sha256` of the local file that was read — never
  the path, never a passage, never a belief statement.

Download them from `https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned`
(prefer `longmemeval_s` or `longmemeval_oracle`),
`https://github.com/snap-research/locomo`, and
`https://huggingface.co/datasets/IAAR-Shanghai/HaluMem`, then run:

```text
uv run agent eval memory-benchmark --external longmemeval \
  --path /local/longmemeval_s.json --sample 40 --seed 7 \
  --output /local/longmemeval-metrics.json
uv run agent eval memory-benchmark --external locomo \
  --path /local/locomo10.json --principal-speaker a \
  --output /local/locomo-metrics.json
uv run agent eval memory-benchmark --external halumem \
  --path /local/halumem_medium.json --sample 5 --seed 7 \
  --output /local/halumem-metrics.json
```

`--sample N --seed S` draws a subset — spread across LongMemEval's question
types — and one seed always draws the same instances. `--output` is required and
is never overwritten. The results document is **informational**: no gate reads
it, it is not activation evidence, and it is not comparable with the published
leaderboards. Adding `--no-deterministic-only` under `RUN_LIVE_MODEL_TESTS=1`
asks each probe once against a real model, under the same USD 4.00 invocation
ceiling enforced before admission.

These adapters add the metric the corpus cannot express: **evidence-provenance
recall**, the fraction of probes for which a returned belief was formed from the
dataset's own evidence turn, falling back to session granularity when the turn
is unknown. It is the difference between recalling the right fact and recalling
it for the right reason.

For LongMemEval and LoCoMo it is also the *only* recall figure to read. Those
datasets name evidence turns rather than corpus labels, so every probe carries
an empty `needed`, and the deterministic block therefore reports
`needed_total = 0`, `needed_recalled = 0`, and `noise_total = returned_total`.
Those numbers are **undefined without labels**, not a nought-per-cent recall and
a hundred-per-cent noise; read `evidence_recalled / evidence_total` instead.
HaluMem is a partial exception: its memory points become `LabeledBelief` values
evaluated by `score_formation`, so its formation counts retain their labeled
meaning. Its probes still carry empty `needed` lists, so `needed_total = 0` and
`noise_total = returned_total`; those probe-level counts are undefined just as
they are for LongMemEval and LoCoMo.

The caveats travel with the numbers, inside the document:

- for probes that name no labels — LongMemEval, LoCoMo, and HaluMem — the `needed_*`
  and `noise_*` counts are undefined and evidence-provenance recall is the
  recall figure;
- there is no model judge; the published LongMemEval figures use one;
- the baseline candidate extractor is deterministic and regex-based;
- answers are scored by normalized token F1 and counted correct at 0.5;
- LongMemEval's single-session-assistant questions are **excluded by design** and
  reported separately — their answers lie in assistant turns, and this platform
  never forms a belief from an assistant turn;
- for LoCoMo one speaker is the principal and the only formation source, the
  other speaker's turns are replayed as non-source messages, and a question is
  scored only when every `dia_id` it cites is one of the principal's turns; the
  category numbers are mapped by the dataset's documented meaning and the raw
  number is kept on every probe;
- HaluMem memory points carry no subject, so their labeled beliefs are subjected
  to the user, and their questions name no evidence turn;
- a scenario carries at most ninety-nine sessions and six probes, so a variant
  with more sessions per instance is refused rather than truncated.
