# ADR-0230: The longest verify jobs split across executors

- Status: Proposed (requested by the repository owner, 2026-10-20)
- Amended by: ADR-0257 (2026-10-26), which replaces decisions 1 and 2 with
  one `apple-ios` job on two nodes, each running both devices
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

1. The simulator job becomes `apple-ios-device` with a `device` parameter,
   `iphone` or `ipad`, and the verify workflow runs it as `apple-ios-iphone`
   and `apple-ios-ipad`. A step-only job keeps the name `apple-ios`, requires
   both, and is what packaging requires, so the `apple-ios` status that
   `main`'s branch protection requires still means both devices passed.
   `make test-apple-ui-ios` reads `IOS_UI_DEVICES`, which defaults to
   `iphone ipad`, and builds for, boots, and tests only the named devices, so
   local use is unchanged.
2. Each device job also sets `parallelism: 2`. The suite is one XCTest class,
   and Xcode's parallel testing divides only by class, so the target reads
   `IOS_UI_SHARD=INDEX/TOTAL`: after the build it lists the enabled cases with
   `xcodebuild -enumerate-tests` and `scripts/ios_ui_shard.py` keeps every
   TOTAL-th one, in name order, from INDEX. The shards of one run together
   cover every enabled case, and an empty listing fails the run. Without the
   setting, the whole suite runs as before.
3. `static` and `integration` set `parallelism: 2`. Smarter Testing's free
   static splitting divides each partition's test files by recorded timing, so
   the two nodes together run the whole partition with no credit-charged
   feature. Each `integration` node has its own PostgreSQL container.
4. `static` runs its lint, type, deployment, documentation, and client-build
   steps on node 0 only. The reading-lane floor runs on both nodes.
5. The exact-tree record of ADR-0114 is written per node, keyed
   `verified-v2-<job>-<node>-<tree>`, and names the split as
   `<job>-<node>-of-<total>-<tree>`. On `main` a job halts only when every node
   of its current split has a record. Each run of a split covers the whole
   partition, so the records of one passing split prove the whole partition
   passed. A job runs on at most two nodes; raising that needs another restore
   step. The new key starts empty, so the first `main` pipeline after this
   change runs the suite in full.

## Consequences

- Requested pipeline 1302 measured the device split alone: `integration` took
  12.3 minutes and `static` 12.7, but each device still took about 30
  (iPhone 1819 seconds, iPad 1832), as long as both together had on one
  machine. The cases themselves, not a shared machine, set the time, which is
  why decision 2 splits them.
- Requested pipeline 1310 measured the case split: each of the four shards
  ran 36 of the 72 enabled cases in 989 to 1057 seconds, the device jobs took
  17.4 and 17.8 minutes, `integration` 14.0 and `static` 11.2, and the whole
  verify workflow 17.9 minutes against a median of 30.4 before.
- The simulator lane uses four macOS executors, each building the UI-test
  products once, so its macOS minutes rise by about three builds.
- A flaky device reruns alone when the workflow is rerun from failed.
- Two nodes of `static` and `integration` each pay their own spin-up and
  install, about a minute of credits per node.
