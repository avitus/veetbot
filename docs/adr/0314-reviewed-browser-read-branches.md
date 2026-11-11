# ADR-0314: Forward-only branches in reviewed browser workflows

- Status: Accepted under the owner's browser work-package continuation
- Date: 2026-11-11
- Related: ADR-0156, ADR-0164
- Detailed design: `docs/plan/browser-automation.md`

## Decision

Extend reviewed workflow observe steps with an optional closed `branches` object.
It names `satisfied` and `failed` destinations as strict zero-based integer step
indexes. Each destination must be strictly after the current step and no greater
than the recipe length. A destination equal to the length ends the workflow:
`satisfied` ends verified, while `failed` ends with an explicit `observed_failure`.
No backward edge, loop, arbitrary expression or page-authored destination is
accepted. The existing 16-step, 64-call, 900-second and run budgets remain.

Branching requires a postcondition with explicit positive failure evidence.
Only a fresh, successful observe result on the recipe origin may select a
branch. Missing, truncated or ambiguous evidence, provider errors and sign-in
interruptions cannot choose a destination. They retain the existing bounded
stop or authentication continuation. A `failed` condition means the declared
failure evidence was observed; missing success alone is not failure evidence.

Branches are forbidden on actions and their uncertain-effect reconciliation.
An action reached through a branch still needs a new observation, a unique
current target, current policy/grants and the ordinary approval path. Failed
verification of an already dispatched action remains read-only and cannot
route to another action through a branch.

Persist the next step independently of the count of completed steps, together
with the last branch's source, closed result and destination. Checkpoint this
choice before proposing further work. Skipped steps never count as completed.
Resume preserves this cursor and all existing definition, profile, generation,
agent and policy fences. A generation change restarts an unfinished read at
its selected step, not at a skipped step.

Legacy recipes omit the new field from their definition digest, preserving
existing checkpoints and catalog behavior. Changing a branch changes the digest.
The deployed recipe catalog is unchanged: this adds reviewed execution mechanics,
not a new activated site workflow, a public tool or model-authored recipe surface.
No permissions, security requirements, milestone or engineering-plan requirement
change. Broad W6 efficiency and live-site acceptance remain separate.

## Verification

Start with a valid reviewed branch rejected by the closed recipe schema, then
prove the runtime initially fails to skip an unnecessary action. Cover positive
alternatives, ambiguity/absence, terminal observed failure, malformed/looping
routes, normal approvals, cancellation, generation/definition changes, bounded
stops, persisted branch recovery and uncertain writes. Real Chromium fixtures
must count zero effects for an already-complete page and one for a necessary,
individually approved action. Verify legacy recipe digests and full browser
regressions in the local Linux sandbox.

Construction evidence in the local Linux ARM64 sandbox, 2026-11-11:

- Red: `make test-static
  PYTEST_ARGS=tests/unit/test_browser_workflow_branches.py` first failed because
  the closed recipe schema rejected a valid forward branch. After schema support,
  the runtime regression failed because an already-ready report still requested
  action approval. Both now pass.
- The focused workflow suite passed 53 cases. It covers strict forward bounds,
  invalid types and modes, missing predicates, terminal observed failure,
  ambiguous/absent evidence, definition identity and preserved generation,
  definition, cancellation, time and call-count fences after branch selection.
- `make test-browser-cases
  CASES=tests/unit/test_browser_workflow_branches_real_chromium.py` passed all
  seven cases. Server counters show zero effects for an already-complete page,
  one for an individually approved action, and no duplicate after response loss
  or failed action verification. A branch that skips an intermediate step keeps
  its selected cursor across approval and counts only executed steps.
- A fixed pre-change digest for the legacy saved-report recipe remains equal.
  Changing a branch destination changes the definition digest. No shipped
  recipe, public tool schema, dependency or permission was changed.

Broader checks passed 1,284 browser-related static tests and 170 browser
contracts, plus lint, type checking, citation reconciliation and strict
documentation checks. The deployed recipe catalog and milestone gates are
unchanged; this evidence covers construction rather than live-site acceptance.

Final browser verification passed all 502 cases with no skips in 440.61 seconds,
using two workers and the established 64 MiB process stack. The full run emitted
no ASGI pending-request teardown diagnostic. No milestone or production acceptance
claim follows from these synthetic checks.
