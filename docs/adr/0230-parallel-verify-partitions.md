# ADR-0230: The longest verify jobs split across executors

- Status: Proposed (requested by the repository owner, 2026-10-20)
- Date: 2026-10-20
- Related: ADR-0107, ADR-0114, ADR-0228
- Detailed design: `docs/plan/development-toolchain.md`

## Context

No verify job requires another, so a requested run lasts as long as its
longest job. In 16 requested pipelines from 1139 to 1267, the median run took
30.4 minutes. `apple-ios` took 30.1 minutes in one step that builds once and
tests the iPhone and iPad simulators at the same time on one machine.
`integration` took 20 to 25 minutes on a small executor with no parallelism,
`static` 17 to 19, `apple` 15, and `browser` 12. All three confirmed flakes in
the sample were on the macOS jobs; each one cost a full rerun of its job.
ADR-0228 left job parallelism as a separate decision.

## Decision

1. `apple-ios` takes a `device` parameter, `iphone` or `ipad`, and the verify
   workflow runs it as `apple-ios-iphone` and `apple-ios-ipad`. Packaging
   requires both. `make test-apple-ui-ios` reads `IOS_UI_DEVICES`, which
   defaults to `iphone ipad`, and builds for, boots, and tests only the named
   devices, so local use is unchanged.
2. `static` and `integration` set `parallelism: 2`. Smarter Testing's free
   static splitting divides each partition's test files by recorded timing, so
   the two nodes together run the whole partition with no credit-charged
   feature. Each `integration` node has its own PostgreSQL container.
3. `static` runs its lint, type, deployment, documentation, and client-build
   steps on node 0 only. The reading-lane floor runs on both nodes.
4. The exact-tree record of ADR-0114 is written per node, keyed
   `verified-v2-<job>-<node>-<tree>`, and names the split as
   `<job>-<node>-of-<total>-<tree>`. On `main` a job halts only when every node
   of its current split has a record. Each run of a split covers the whole
   partition, so the records of one passing split prove the whole partition
   passed. A job runs on at most two nodes; raising that needs another restore
   step. The new key starts empty, so the first `main` pipeline after this
   change runs the suite in full.

## Consequences

- A requested run should be bounded by `apple` (about 15 minutes) instead of
  `apple-ios` (about 30). The first requested run on this configuration is the
  evidence.
- Each device builds the UI-test products on its own executor, so macOS
  minutes rise by one build.
- A flaky device reruns alone when the workflow is rerun from failed.
- Two nodes of `static` and `integration` each pay their own spin-up and
  install, about a minute of credits per node.
