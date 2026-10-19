# ADR-0219: Exact-revision signing for the isolated recall release

- Status: Accepted within the owner's explicit recall release authorization
- Date: 2026-10-18
- Related: ADR-0074, ADR-0107, ADR-0216, ADR-0218
- Detailed design: `docs/plan/development-toolchain.md`

## Context

The owner explicitly directed opening the recall pull request when ready and
following it through production deployment. The separate instruction to avoid
pushing to `dev` while existing pull requests remain open still applies. The
release therefore uses `codex/recall-production-release`, preserving current
production fixes and the independently measured source identities.

Ordinary verification includes the real, nonpublishing Apple signing smoke on
the reviewed candidate. Historical signing results on another revision cannot
establish this candidate's readiness. The completed historical exception in
ADR-0216 is not a standing grant to this branch.

## Decision

The workflow may select `dev` and the single named
`codex/recall-production-release` branch for `apple-signing-smoke`. Non-main
pipelines still require `run_verify: true`; delivery and TestFlight publication
remain restricted to `main`. This selector does not grant access to credentials.

After CodeRabbit finishes reviewing the exact candidate, the release operator
may temporarily admit only that branch and its exact `pipeline.git.revision`
in the installer context. Preserve existing `main`/`dev` alternatives, project
and group restrictions, and the denials for SSH jobs and API-sourced
configuration. No wildcard branch grant, copied credential or uploaded
configuration is permitted.

Retain original restriction readbacks. Add and verify the temporary expression
before removing the original expression. Run ordinary committed configuration
and verify the pipeline revision and all required jobs. On completion, failure
or cancellation, add and verify the original expression before removing the
temporary expression. Retain proof of restoration. Any reviewed repair needs
its own exact-revision admission; it cannot inherit a previous revision's grant.

## Verification and consequences

Red-first workflow tests require exactly the two named smoke branches while
retaining all main-only delivery filters and existing package/no-upload checks.
Live context readbacks and real hosted signing are still required: repository
tests alone cannot prove them. This does not waive collection, quality gates,
independent human review, production input compatibility or the `dev` push hold.
