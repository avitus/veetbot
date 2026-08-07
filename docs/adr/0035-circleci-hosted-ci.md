# ADR-0035: CircleCI as the hosted CI provider

- Status: Accepted
- Date: 2026-07-31
- Related: Milestone 0, Section 21, ADR-0025
- Detailed design: `docs/plan/development-toolchain.md`

## Context

ADR-0025 fixed the invariant that hosted CI is one workflow definition with
four Makefile-backed partitions, but its implementation specification selected
GitHub Actions. The repository owner selected CircleCI before Milestone 0 was
completed. Keeping the GitHub-specific file would either ignore that decision or
create two hosted definitions of a green build.

## Decision

CircleCI is the only hosted CI provider. `.circleci/config.yml` uses
configuration version 2.1 and defines the same four partitions required by
ADR-0025: static, contract, integration, and live. The first two remain the
partitioned contents of `make check`; integration uses `postgres:16-alpine` as a
secondary container; live tests run only from a nightly workflow or a manually
triggered pipeline with `run_live: true`.

Provider credentials are supplied through a restricted CircleCI context named
`live-model`. Python stays at 3.12 without a matrix, the dependency cache remains
keyed by `uv.lock`, and every verification command remains a Makefile target.
GitHub Actions workflow files are removed so a second CI definition cannot
drift from CircleCI.

## Consequences

- The repository must be connected to a CircleCI project before hosted evidence
  can be recorded.
- The `live-model` context must exist before a live workflow can run.
- Pull requests and branch pushes receive the static, contract, and integration
  partitions through the ordinary CircleCI VCS pipeline.
- ADR-0025 remains authoritative for the partition semantics; this decision
  supersedes only its GitHub Actions provider choice.

## Alternatives considered

- **Retain GitHub Actions beside CircleCI:** rejected because two CI definitions
  would violate ADR-0025's single-definition invariant.
- **Inline commands directly in CircleCI:** rejected because the workflow would
  become a second definition of repository checks.
- **Run live tests on pull requests:** rejected because forked pull requests must
  not receive provider credentials and live runs may incur cost.
