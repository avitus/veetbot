# ADR-0211: Commit-scoped reliability signing

- Status: Accepted by the owner on 2026-10-16
- Date: 2026-10-16
- Related: ADR-0074, ADR-0107, ADR-0199, ADR-0201, ADR-0210
- Detailed design: `docs/plan/development-toolchain.md`

## Context

PR #163's release candidate is on `reliability`, while `dev` contains deliberately
deferred work for a separate release. The ordinary signing smoke and installer
context previously admitted only `dev` and `main`. Rewinding `dev`, importing the
deferred work or borrowing signing evidence would violate the agreed scope.
The owner explicitly approved commit-scoped signing for this release, followed
by restoration of the original restriction. This is separate from the narrow
OpenAI repair approval and does not extend ADR-0201's foundation-only exception.

## Decision

The existing nonpublishing `apple-signing-smoke` workflow invocation may select
`dev` or `reliability`. Non-main pipelines still require `run_verify: true`.
All verification jobs, packaging scripts, signing checks, main-only delivery
filters and absence of publication credentials in the smoke remain unchanged.

Only after actual CodeRabbit review of the final candidate, the designated
reliability operator may temporarily amend the external installer-context
expression to admit `reliability` **and** that exact `pipeline.git.revision`.
The existing `main`/`dev` alternatives, project and group restrictions, denial
of SSH jobs and denial of API-sourced configuration remain intact. A branch
filter alone never grants access to signing keys. No general reliability-branch
grant, uploaded configuration, secret copying or check waiver is authorized.

The operator retains the original restrictions and verifies current branch/PR
identity. Add and verify the replacement expression before removing the original
expression, so both constraints overlap rather than leaving an unrestricted
interval. Request one ordinary pipeline from committed `reliability` configuration
and verify its actual revision and every required job, including real signing.
As soon as that attempt ends, including failure or cancellation, add and verify
the original expression before removing the temporary one. Retain readbacks of
both transitions and restoration. A subsequent reviewed repair needs another
exact-revision admission within this approved release scope; the previous
revision's grant or evidence cannot certify it.

## Verification and consequences

Red-first workflow regressions pin only the two allowed smoke branches and
preserve all main-only delivery filters and nonpublishing package assertions.
Repository verification cannot prove external context restrictions or live
signing: operator readbacks, restoration evidence and the exact-head hosted
pipeline must do that. No production release is authorized by this test alone.

`dev` history and the separate PR #169 remain unchanged. This route satisfies
ordinary signing on the actual release candidate; it does not reuse foundation
evidence. The full memory confirmation, independent human review, all quality
gates and the USD 300 aggregate cap remain unchanged. Neither a successful
signing job nor this decision establishes recall quality or permits failed
evaluation observations to be reused.
