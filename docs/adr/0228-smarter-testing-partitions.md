# ADR-0228: CircleCI Smarter Testing runs three pytest partitions

- Status: Proposed (requested by the repository owner, 2026-10-20)
- Date: 2026-10-20
- Related: ADR-0107, ADR-0114, ADR-0156
- Detailed design: `docs/plan/development-toolchain.md`

## Context

The `static`, `contract`, and `integration` jobs ran their Make targets directly
and passed a JUnit path through `PYTEST_ADDOPTS`. In the requested full runs of
October 2026 (pipelines 1265 to 1276), `static` took about 19 minutes,
`integration` 22 to 26, and `contract` about 4.

CircleCI Smarter Testing wraps a test command. `.circleci/test-suites.yml`
names a suite, a `discover` command that lists test atoms, and a `run` command
that executes the atoms it is given; `circleci testsuite run "<suite>"` replaces
the job's test step. Three features are free: static test splitting by timing
when a job sets `parallelism`, rerunning only the failed tests when a workflow
is rerun from failed, and test-result uploads. Test impact analysis, dynamic
test splitting, dynamic parallelism, and automatic reruns are charged per test
atom.

The toolchain specification states that no test-result cache or reduced test
selection replaces a required check.

## Decision

1. `.circleci/test-suites.yml` defines the suites `static`, `contract`, and
   `integration`, one for each job, and each of those jobs runs its tests with
   `circleci testsuite run "<job>"`.
2. Each suite discovers and runs through the job's own Make target. Discovery is
   `make -s test-<job> PYTEST_ARGS="--collect-only -q"`, reduced to the unique
   test files. The run is `make test-<job>` with the selected files and the JUnit
   path in `PYTEST_ARGS`. The marker selection stays in the Makefile alone, and CI
   still invokes the Make targets of the job table. An empty `PYTEST_ARGS`, the
   default, runs the whole partition, so local use is unchanged.
3. A test atom is a test file. The run writes `junit_family=xunit1` reports, so
   every case names its file, and results map back to atoms. Reports land in
   `test-results/<job>/`, and the job's existing `store_test_results` step
   uploads them.
4. Only the free features are used. No suite sets `options` or `analysis`, and
   no job's `parallelism` changes, so every run of a job executes its whole
   partition. Rerunning a workflow from failed runs only the files that failed
   in the earlier attempt on the same commit; together the attempts cover the
   partition, and ADR-0114's tree record is still written only by an attempt
   whose last step passes. A full workflow rerun runs everything.
5. `sandbox` (one test file), `browser` (ADR-0156 rejects a baseline that did
   not run every browser case in one session), `browser-image`, the Apple jobs,
   and `public-site` keep their direct steps.
6. Enabling a credit-charged feature or job parallelism is a separate decision.
   Test impact analysis in particular would reduce the selection of a required
   check, which the toolchain specification forbids without an amendment.

## Consequences

- Discovery collects the partition once more before it runs. Locally that
  collection takes about 30 seconds for `static` and about 27 for `contract` and
  `integration`, against jobs of 4 to 26 minutes.
- Setting `parallelism` on one of these jobs later splits its files by recorded
  timing with no other change; each `integration` node gets its own secondary
  PostgreSQL container.
- `circleci testsuite doctor "<suite>"` validates a suite locally. On a Mac,
  the `integration` partition includes native bridges that drive Xcode, which
  skip on CI's Linux executor; run its doctor with
  `PYTEST_ADDOPTS=--ignore=tests/native` and a disposable database.
- The executor's `circleci` command provides `testsuite`, as CircleCI documents
  for every job; the first hosted run on this configuration is the evidence.
