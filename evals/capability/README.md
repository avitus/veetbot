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
RUN_LIVE_MODEL_TESTS=1 agent eval capability --suite research
```

Without the live flag, the command skips without loading credentials or making
provider calls.
