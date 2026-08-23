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
artifact. The 24 provider calls have a combined USD 1.20 policy ceiling. Startup
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
