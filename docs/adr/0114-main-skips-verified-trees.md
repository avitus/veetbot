# ADR-0114: `main` does not reverify a source tree that already passed

- Status: Proposed
- Date: 2026-09-21
- Related: ADR-0035, ADR-0048, ADR-0049, ADR-0074, ADR-0107
- Amends: ADR-0107 decision 1, which reran every verification job on `main`
- Detailed design: `docs/plan/development-toolchain.md`

## Context

ADR-0107 requires one requested `run_verify` pipeline on the exact head of a
change proposed for `main`, and branch protection requires its seven
verification contexts before the merge. The `main` pipeline then runs the same
verification jobs again before it packages and delivers.

A promotion merges `dev` into `main` with a merge commit. On 2026-09-21 the
five most recent merge commits on `main` each had exactly the same source tree
as the pull-request head the requested run had verified. The second run tested
identical files, including both macOS jobs, which ADR-0107 measured at about
96% of a verification pipeline's cost, and it delayed production by the length
of the verification stage.

The second run is not always redundant. Branch protection does not require a
pull request to be up to date with `main`, and the owner keeps an admin
override. In either case the merge commit can hold a tree nothing verified,
and ADR-0107 names the `main` pipeline as the backstop for that.

## Decision

1. Each verification job keyed to the source tree — `static`, `contract`,
   `integration`, `sandbox`, `apple`, and `apple-ios` — writes a CircleCI cache
   entry named for the job and the commit's tree hash, after its last step has
   passed. CircleCI saves a cache only when every earlier step succeeded, and a
   key cannot be overwritten once written.
2. On `main`, each of those jobs restores that entry right after checkout. If
   it finds its own record for the current tree, the job halts successfully
   before installing or testing anything. On every other branch the job always
   runs in full, so the requested pre-merge run remains real evidence.
3. The record is keyed by tree, not commit. A merge commit that reproduces a
   verified head skips; a merge whose tree differs from every verified tree,
   including one made after `main` moved or through the admin override, runs
   the full suite as before.
4. `static` runs the reading-lane floor before it consults the record, because
   that check judges the commit range and its trailers, not the tree.
5. `public-site` always runs, because `package-release` packages its output.
   Packaging, deployment and TestFlight delivery are unchanged and still
   require all seven verification jobs to report success.

## Consequences

- A promotion whose merge commit keeps the verified tree reaches packaging
  after the verification jobs' checkout and cache restore rather than after
  their full runs. The macOS executors still start, but they stop within
  seconds.
- A test that passed once on a tree is not run a second time on `main`. A
  flaky test is therefore not given a second chance to fail before delivery;
  flakes are fixed, not caught by repetition.
- The CircleCI cache becomes part of the release gate. Anyone who can change
  the committed CircleCI configuration and trigger a pipeline could write a
  false record, but that person can already change what the verification jobs
  run and what the pull request's statuses report. The trust boundary does
  not move. CircleCI shares the upstream cache with pull requests from forks,
  so the project's setting that leaves forked pull requests unbuilt is now also
  part of this boundary; turning it on requires revisiting this decision.
- CircleCI keeps caches for at most fifteen days. A promotion merged later than
  that after its requested run reverifies in full.
- Requiring pull requests to be up to date with `main` in branch protection
  would make skips more frequent but is not needed for safety. That setting
  belongs to the owner.

## Alternatives considered

- **Read the pull-request head's commit statuses from GitHub.** This needs a
  new token in a CircleCI context, because unauthenticated GitHub API limits
  are per IP address and CircleCI hosts share addresses. It also misses squash
  and fast-forward merges, whose relationship to the verified head is not a
  parent link.
- **Dynamic configuration with a setup workflow.** This could drop the skipped
  jobs from the pipeline entirely, saving the macOS start-up too. It changes
  how every pipeline is built and puts a setup job on the critical path of
  each one, including the requested pre-merge run.
- **Deliver on `main` without any verification jobs.** This is the largest
  saving, but it removes the backstop ADR-0107 keeps for a merge whose tree
  nothing verified.
