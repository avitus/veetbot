# ADR-0107: Hosted verification is automatic on `main` and on request elsewhere

- Status: Proposed
- Date: 2026-09-17
- Related: ADR-0025, ADR-0035, ADR-0048, ADR-0049, ADR-0074
- Supersedes in part: ADR-0035's consequence that every branch push receives
  the verification partitions
- Detailed design: `docs/plan/development-toolchain.md`

## Context

ADR-0035 gave every branch push the hosted verification partitions. That was
cheap while the workflow held three Linux jobs. ADR-0049 and ADR-0074 later
added two macOS test jobs and a macOS signing smoke, and `dev` became the
branch that receives every finished change directly.

In the seven days before 2026-09-17 the project ran 122 pipelines: 96 on
`dev`, 24 on `main` and 2 elsewhere. A `dev` pipeline cost roughly 3,900
credits, estimated from job durations and CircleCI's published rates, and the
three macOS jobs were about 96% of it. All five Linux jobs together were about
140 credits. Of the 97 `dev` verification workflows, 59 passed, 16 failed and
22 were cancelled by a newer push after their macOS executors had already
billed minutes.

The repository already has a cheaper gate for the same checks.
`.chunk/config.json` runs `make check` on a disposable CircleCI sidecar, and
`docs/plan/development-toolchain.md` records that a passing remote `make check`
satisfies the same criterion as a local run.

## Decision

1. The `verify` workflow starts by itself only on `main`. The condition reads
   the pipeline's branch, not what created the pipeline, so a push to `main`
   and a pipeline requested for `main` both run it. There it still gates
   packaging, deployment and TestFlight delivery exactly as ADR-0048 and
   ADR-0074 define.
2. On every other branch a pipeline starts no workflow by itself. The workflow
   runs there only when the pipeline is triggered with the boolean parameter
   `run_verify: true`. `run_live: true` still selects the live workflow and
   wins when both are set, on `main` as well.
3. The gate for a change entering `dev` is `chunk validate` on the sidecar,
   plus the local suites the sidecar cannot run when the change touches them:
   integration against the disposable PostgreSQL, the sandbox gates, and the
   Xcode suites.
4. A change proposed for `main` still needs hosted verification on its exact
   final head. The proposer requests one `run_verify` pipeline for that commit
   and the pull request shows its statuses. The repository contract's review
   gate is unchanged in strength; only the way the run starts changes.
5. The Apple signing smoke keeps its `dev`-only filter and therefore runs in a
   requested `dev` pipeline. The restricted signing context still excludes
   unversioned configuration; a requested pipeline uses the committed file.
6. The rule is one workflow condition in `.circleci/config.yml`, pinned by
   `tests/unit/test_toolchain.py`. No job list is duplicated and no CircleCI
   project setting carries the policy.

## Consequences

- Hosted credits are spent once per promotion and once per `main` push instead
  of once per push to any branch.
- A failure only the hosted jobs can see, such as an iPhone UI regression,
  surfaces at the requested run rather than minutes after the push that caused
  it. The cause is then one of several commits, not one.
- A push to `dev` no longer proves anything by itself. Evidence recorded for a
  `dev` commit names the sidecar or local run that produced it.
- A requested pipeline has no previous revision, so the reading-lane floor
  falls back to `origin/main` as its base, which is the range a promotion
  should be judged on.
- Anyone who merges to `main` without requesting the run is caught by the
  `main` pipeline, which verifies before it delivers, but only after the merge.
  GitHub does not enforce the requested run: `main` carries no branch
  protection, and carried none under ADR-0035 either, so the exact-head gate
  was procedural before this decision and stays procedural after it. Requiring
  the verification statuses on `main` would make GitHub refuse such a merge. It
  is a repository setting, outside the configuration this repository can test,
  and enabling it is the owner's decision.

## Alternatives considered

- **Keep the Linux jobs automatic and put only macOS on request.** It keeps
  about 96% of the saving and automatic integration and sandbox coverage.
  Rejected by the owner in favor of one rule; it also needs a second workflow
  that repeats the job list.
- **No hosted verification before the merge.** The `main` pipeline already
  verifies before delivery. Rejected because it weakens the contract's
  exact-head requirement and turns `main` red for failures a requested run
  would have caught.
- **CircleCI's "only build pull requests" project setting.** Rejected because a
  `dev`-to-`main` pull request is open much of the time, so most pushes would
  still build, and the policy would live outside the repository where no test
  can pin it.
- **An approval job ahead of the workflow.** Rejected because job requirements
  cannot differ by branch inside one workflow, so it needs a duplicated
  workflow, and every push would leave a pending status on the commit.
- **Skip the macOS jobs when no Apple file changed.** A separate saving that
  also applies to `main`. It needs dynamic configuration and a decision about
  the release gate in ADR-0049, so it is not part of this change.
