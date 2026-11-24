# ADR-0347: Verify reviewed browser navigation before advancing

- Status: Accepted under the owner's browser work-package continuation
- Date: 2026-11-23
- Related: ADR-0159, ADR-0164, ADR-0328, ADR-0333
- Detailed design: `docs/plan/browser-automation.md`

## Decision

A reviewed navigation step may declare an optional `postcondition` using the
existing closed browser condition contract. After successful, uninterrupted,
same-origin navigation, the runner queues a separate ordinary
`browser.observe(wait_for=...)` in a persisted `verify_navigation` phase. Only
positive satisfied evidence completes the navigation step and permits later
work. A declared positive failure stops as `observed_failure`; missing,
conflicting, interrupted or unsuccessful evidence cannot complete the step.
Ordinary failures stop `unverified`; authentication uses the existing suspension
and verified-resumption path.

A loaded page, same-origin redirect or quiet DOM alone does not satisfy the
condition. The reviewed condition can describe an expected location, rendered
text, region, control or typed row. It is evidence of the observed page state,
not proof that a navigation caused a business effect or that the site is truthful.
The navigation tool receives only its existing URL argument; completion uses
its own audited read and the run's pinned observe capabilities. Preflight checks
that wait before starting the recipe, including navigation-only recipes.

The wait retains the five-second/21-read bound and ordinary workflow/run call,
step, cancellation and time limits. Verification failure does not retry navigation
or replay earlier actions. Generation changes during a read restart only the
unfinished navigation with fresh evidence; verified earlier steps remain complete.
If verified sign-in already queued exactly that navigation URL, the runner uses
that ordinary call and verifies its result instead of loading the page twice.
A different recovery URL cannot substitute for the reviewed navigation.
An action after navigation still discovers a fresh target and uses ordinary
approval and effect handling. Pending approval resumption and uncertain actions
cannot repeat a previously completed navigation or its wait.

Omitting the condition preserves existing navigation behavior and recipe digests.
A declared condition changes the recipe binding. Navigation does not gain action
fields, preconditions or branch destinations. No public tool schema, deployed
recipe, new dependency, permission, milestone or gate changes. This implements
W4's expected navigation completion and W6's verified step progress within the
existing engineering-plan contract; broader live-site acceptance is separate.

## Verification

Begin with a failing closed-recipe regression, then run execution regressions
before adding the runner phase. Cover positive, missing, conflicting and declared
failure evidence; exact locations; pinned capabilities; unchanged legacy identity;
budgets; authentication resumption; ordinary approval; and lost action responses.
Real Chromium must reject unexpected same-origin redirects before any action,
wait for a delayed expected state, and record no duplicated server effect.

Construction evidence in the local Linux ARM64 sandbox:

- Red: `make test-static
  PYTEST_ARGS=tests/unit/test_browser_workflow_navigation.py` initially rejected
  the declared navigation condition. After admitting the field, six cases failed
  because the verification read and pinned wait-capability checks were missing.
- The first focused navigation/workflow/capability run passed all 77 cases.
  The expanded navigation module passed all 20 cases, covering sign-in resume
  and cancellation after a prior approved effect, exact versus redirected
  recovery URLs, read failures, foreign origins and exhausted time/call bounds.
- The sign-in regression first found an unnecessary second navigation after the
  service had already queued the same URL. Matching recovery calls now pass
  through normal dispatch and verification once; different URLs are not reused.
- `make test-browser-cases
  CASES=tests/unit/test_browser_workflow_navigation_real_chromium.py` passed all
  seven cases. Real Chromium waits for delayed state, accepts the declared
  redirect destination and refuses an unexpected same-origin redirect, missing
  state or conflicting evidence before any approval/effect. Positive failure is
  retained. An approved action with a lost response has exactly one server effect
  and never repeats the completed navigation or its verification.
- After incorporating advancing `dev`, broader verification passed 1,791 selected
  browser/steering/People-context static tests and 174 contracts. Lint and type
  checking passed. Reading lane A matches the diff-derived minimum. Citation
  reconciliation and strict documentation checks passed; no milestone or gate moves.
- The integrated full `make test-browser BROWSER_TEST_WORKERS=2` partition passed
  all 447 cases in 465.96 seconds, with no skips or warnings. These are local
  construction checks; production and broader W4/W6 live-site acceptance remain
  separate, and the deployed recipe catalog is unchanged.
