# ADR-0252: Exact-commit signing for the isolated dreaming release

- Status: Accepted — owner explicitly authorized PR #196 signing on 2026-10-25
- Date: 2026-10-25
- Related: ADR-0074, ADR-0107, ADR-0174, ADR-0219
- Detailed design: `docs/plan/development-toolchain.md`

## Decision

The owner authorized releasing the dreaming verification repair independently
of unrelated `dev` work, including its nonpublishing Apple signing check.
Add only `codex/dreaming-candidate-evidence-release` to the existing smoke-job
branch selector. Delivery and TestFlight publication remain restricted to
`main`; non-main verification still requires `run_verify: true`.

After CodeRabbit reviews the final PR #196 candidate, temporarily admit only
that branch and exact `pipeline.git.revision` in the installer context.
Preserve the existing `main`/`dev` alternatives, project/group restrictions,
and SSH/API-configuration denials. The branch selector alone grants no
credential access. No wildcard, credential copying or uploaded configuration
is authorized.

Retain the original context readback. Add and verify the temporary expression
before removing the original expression. Run the ordinary committed workflow,
verify its exact revision, and restore the original restrictions immediately
after the signing attempt finishes, fails or is cancelled: add and verify the
original expression before removing the temporary one. Retain both transitions
and restoration evidence. A reviewed repair requires a new exact-commit grant
within this PR's release scope; this is not a standing branch exception.

## Verification and boundaries

Red-first workflow tests pin the three named smoke branches, the existing
package/signature checks, absence of upload credentials, and main-only delivery.
Live context readbacks and a successful signing job on the final reviewed head
remain required before merge. All other review, CI and deployment gates remain.
The owner also authorized an immediate bounded supervised dreaming run after
deployment and verification of an actual pending proposal. This does not enable
unattended application or change source, privacy, spend or evaluation rules.
