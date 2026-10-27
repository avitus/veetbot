# ADR-0257: One final verification run, and Apple jobs keyed on their inputs

- Status: Proposed (requested by the repository owner, 2026-10-26)
- Date: 2026-10-26
- Related: ADR-0107, ADR-0114, ADR-0152, ADR-0230, ADR-0236
- Amends: ADR-0107 decision 3 (the `dev` gate), ADR-0114 (records honoured on
  `main` only), ADR-0230 decisions 1 and 2 (the simulator executor layout)
- Detailed design: `docs/plan/development-toolchain.md`

## Context

The project's CircleCI usage, read from the insights API on 2026-10-26:

| Period | Credits per day | `verify` runs per day | Credits per run |
| --- | --- | --- | --- |
| 2026-09-17 to 2026-10-20 | about 30,000 | 5.8 | about 5,200 |
| 2026-10-20 to 2026-10-27 | about 200,000 | 15.4 | about 12,900 |

macOS executors were about 97% of every run. Two changes on 2026-10-20 caused
the increase:

- ADR-0230 moved the simulator suite from one macOS executor to four: one job
  per device, each on two nodes, and every node builds the UI-test products.
  Its own measurement showed that a single device took as long alone (about
  30 minutes) as both devices together on one machine, so the per-device split
  added executors without saving time. The two device jobs cost about 10,700
  credits a run, against about 3,470 for the single job before.
- The review gate in `AGENTS.md` began requesting a `run_verify` pipeline with
  every push of a pull request, alongside each CodeRabbit round, instead of
  once on the final head as ADR-0107 decision 4 had it. Each review round
  became a full hosted run. About 33% of the credits in that week went to
  failed runs and about 10% to runs that a newer push cancelled.

Meanwhile every change entering `dev`, and every review round, also ran the
full `make check` on the chunk sidecar (about 17 to 19 minutes). That is the
same set of checks the requested hosted run repeats. The owner judged both
the CircleCI spend and the time to merge too high, and asked for one final
test run before merging.

The Apple suites read nothing outside `clients/apple`: the Xcode project and
the Swift package hold every source, the UI tests launch the app with their
own fixtures, and the Makefile, the CI configuration, and
`scripts/ios_ui_shard.py` decide how they run. Most changes touch none of
those paths, yet each requested run started both Apple jobs in full.

## Decision

1. **One full verification, on the final head.** A change proposed for
   `main` runs in full once: the requested `run_verify` pipeline on its final
   head, after CodeRabbit reports no findings and after `main` has been merged
   into the branch. While a change is developed or reviewed, and when a small
   change is pushed to `dev`, run the focused tests for what changed plus
   `make lint`, `make typecheck`, and `make docs-check` for documentation. The
   sidecar is for focused browser cases (ADR-0236) and is the fallback for a
   full `make check` when hosted CI is unavailable, not a per-push gate.
2. **One simulator job, two nodes, both devices on each.** `apple-ios` is
   again the macOS job itself, so the status `main`'s branch protection
   requires still names it. It runs on two nodes. Each node builds once,
   boots the iPhone and the iPad simulators, and with
   `IOS_UI_SHARD=INDEX/TOTAL` runs every second case on both devices at the
   same time. The per-device jobs `apple-ios-iphone` and `apple-ios-ipad`, and
   the step-only gate that required them, are removed.
3. **Apple jobs keyed on their inputs.** `skip_verified_tree` takes an
   optional `inputs` list. Without it, the record's identity is the source
   tree, as before. With it, the identity is a digest of the git objects at
   those paths, and a missing path fails the step. `apple` and `apple-ios`
   name `clients/apple`, `Makefile`, `.circleci/config.yml`, and
   `scripts/ios_ui_shard.py`, so a run whose Apple inputs already passed halts
   both Apple jobs right after checkout.
4. **Records hold on every branch.** A verification job halts on any branch,
   not only on `main`, when it finds its own record for its identity. A job
   writes that record only after its last step passes, so a rerun of a
   requested pipeline repeats only the jobs that have not passed that tree,
   and `main` still skips the jobs its pull request's final run passed.

## Consequences

- A run that touches no Apple input bills each Apple job for its start-up and
  checkout only, so its cost falls from about 13,600 macOS credits to roughly
  the Linux jobs plus about a minute on each of the three Apple executors. Its
  wall-clock time is set by the Linux jobs, about 12 to 14 minutes, instead of
  by the simulator suite.
- A run that touches the Apple inputs uses two simulator executors instead of
  four. Each node runs about half the cases on two devices at once, which
  ADR-0230's measurements put at about the 17 to 18 minutes of the four-node
  layout. That figure is an estimate until a requested run measures it.
- Fewer hosted runs: one per promotion plus reruns after a final-run failure,
  instead of one per review round. A failure that only hosted jobs can see,
  such as a simulator regression, surfaces at the final run rather than while
  the review goes on. A fix for it is a new push, a new review, and a new
  final run.
- A push to `dev` proves less by itself than a full sidecar `make check` did.
  A regression that focused tests miss reaches `dev` and is caught by the
  promotion's final run. Evidence recorded for a `dev` commit names the
  focused tests that produced it.
- Including the whole `Makefile` and CI configuration in the Apple identity
  is conservative: a change to an unrelated target or job reruns the Apple
  suites. A narrower identity would save more and would need its own
  evidence that the excluded parts cannot reach the suites.
- A flaky Apple case in a run that changed Apple inputs still reruns its job;
  a rerun from failed repeats only the failed nodes.

## Alternatives considered

- **Keep four simulator executors.** Saves no time over two nodes running both
  devices at once, and costs about twice as much.
- **One simulator executor with no sharding.** Cheapest per Apple-changing run
  (about 3,470 credits) but makes the simulator suite, about 30 minutes, the
  longest job again.
- **Dynamic configuration that never starts the Apple jobs.** Saves the
  start-up minute too. A job that never runs reports no status, so `main`'s
  required `apple` and `apple-ios` statuses would block every merge without a
  stand-in job, and the policy would move into a setup workflow. The halt keeps
  one workflow and the required statuses.
- **Keep the sidecar `make check` as the `dev` gate.** It repeats the final
  hosted run's checks on every push. The owner asked for one final run.
