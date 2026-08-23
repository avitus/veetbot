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
