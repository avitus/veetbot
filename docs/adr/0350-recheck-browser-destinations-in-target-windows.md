# ADR-0350: Recheck browser destinations in later target windows

- Status: Accepted under the owner's browser work-package continuation
- Date: 2026-11-24
- Related: ADR-0164, ADR-0315, ADR-0327, ADR-0347
- Detailed design: `docs/plan/browser-automation.md`

## Decision

A reviewed action directly after a conditioned navigation may opt into
`recheck_destination: true`. The strict boolean defaults to false, preserving
ADR-0347's first-window-only behavior and existing definition digests. True
changes the recipe binding. It is valid only on an action whose preceding
recipe step is a conditioned navigation with a control, text, region or location
predicate. Typed-row predicates are excluded: their separate extraction cannot
prove the capture that supplies an action's target. A branch that skipped the
navigation retains ADR-0347's existing no-inherited-destination behavior.

The first target read still uses the pinned ordinary `wait_for`. Later region
focus and advertised control continuations use their existing closed tool
arguments and revision/document fences. Before accepting a later capture's
region, target or next continuation, the runner evaluates the destination
condition against that capture's bounded content. It uses the same pure condition
evaluator as the ordinary completion checker. A returned condition-status field
cannot substitute for this evaluation. The runner performs no polling, extra
capture or extraction for this local check, and never reuses a target from an
older window. Missing or conflicting evidence stops `unverified`; positive
declared failure stops `observed_failure`, before approval or dispatch.

Complete normalized text lines, non-truncated regions, exact locations and
unique role/name controls with the declared state retain their existing matching
rules. Text and region truncation and focus boundaries remain visible limits;
a marker outside the returned window cannot be inferred from earlier evidence.
Later checks require text-coverage metadata for any text success or failure
predicate. Location predicates stop if the returned URL occupies at least 4,093
UTF-8 bytes: the 4,096-byte output limit may have clipped a partial character
without indicating URL truncation. These conservative projection checks do not
change the ordinary checker's evaluation of provider evidence before projection.
Success and failure are evaluated in that window, not across historical captures.
This is bounded visible evidence, not proof of whole-page completeness or truth.

The existing four-window target allowance, current policy, approval, cancellation,
workflow/run budgets, definition/generation binding and uncertain-effect rules
remain. A new window spends the same persisted allowance. Approval still binds
the target and revision just proved; no already-approved target is substituted.
After dispatch only existing read-only reconciliation is allowed, never another
target search or write. No public tool schema, deployed recipe, dependency,
permission, milestone or gate changes.

This resolves ADR-0347's open local-check alternative for reviewed opt-in actions
without changing the default restriction. Broader multi-capture/typed-row proof
and site acceptance remain separate. No engineering-plan requirement is weakened.

## Verification

Start with failing recipe and execution regressions. Prove that unchanged later
windows reach ordinary approval; changed, truncated, ambiguous, interrupted or
foreign evidence cannot do so; forged/stale status flags cannot replace evidence;
and the four-window bound remains. Retain the original default-stop regressions.
Real Chromium must cover focused and continued targets, in-document mutation and
history changes, ordinary approval, replacement after approval and response loss,
with server counters proving zero unauthorized effects and no duplicate writes.

Construction evidence in the local Linux ARM64 sandbox:

- Red: `make test-static
  PYTEST_ARGS=tests/unit/test_browser_workflow_destination_recheck.py` initially
  rejected the opt-in field. After admitting the closed contract, all seven
  execution cases still stopped at the first window. They now follow later
  windows only with current positive proof and retain the four-window ceiling.
- The initial focused suite passed 88 cases; the expanded workflow/discovery/
  region/completion suite passed 148. Coverage includes control, text, region and
  location evidence, duplicate/disabled controls, truncated text/regions,
  foreign origins, forged status flags, strict types, row exclusion and legacy
  definition identity. The original default-stop tests are unchanged.
- `make test-browser-cases
  CASES=tests/unit/test_browser_workflow_destination_recheck_real_chromium.py`
  passed all eleven cases. Real document/open-shadow focus and control
  continuations reach ordinary approval with current proof. In-document changes,
  history moves, declared failure and conflicting evidence stop before effects.
  Replacing an approved node produces no effect; response loss produces exactly
  one server effect and does not restart discovery.
- After incorporating advancing `dev` and projection guards, 1,825 selected browser/steering/recall
  static tests and 171 contracts passed, including opt-in approval resumption
  fences for changed generation/definition, cancellation and expiry. Lint and
  type checking passed. Citation reconciliation and strict documentation checks
  passed; reading lane A matches the diff-derived minimum.
- Further red regressions demonstrated that missing text coverage and a clipped
  multibyte URL could each incorrectly reach approval. Later-window rechecks now
  reject both, including missing failure-predicate coverage and four-byte URL
  characters at the projection boundary.
- The final full `make test-browser BROWSER_TEST_WORKERS=2` run passed all 462
  real-browser cases, including all eleven new cases and the unchanged
  default-stop regressions. The final selected static suite includes all 26
  destination-recheck contract and execution cases. Architecture and lane checks
  passed. These are local sandbox results, not deployed-site acceptance.
