# ADR-0315: Bounded control discovery for reviewed workflows

- Status: Accepted under the owner's browser work-package continuation
- Date: 2026-11-11
- Related: ADR-0157, ADR-0163, ADR-0164, ADR-0314
- Detailed design: `docs/plan/browser-automation.md`

## Decision

When a reviewed action's fresh observation contains no enabled exact role/name
match, follow the result's advertised `next_observe` through the ordinary
`browser.observe` tool pipeline. Accept only the closed `after` or `cursor`
request: an `after` reference must be in that observation's controls, and a
cursor must equal its coverage cursor. Never synthesize offsets, use text or
region continuations, or substitute the coverage cursor for a projected prefix's
advertised `after`. Unsupported or malformed continuation ends discovery safely.

Each action step may request at most four target windows, including its initial
read. Persist the step index and requested-window count before dispatch; retain
that allowance through suspension, generation changes and worker resumption.
A new action step receives its own allowance. Existing separately audited
recovery reads remain within their existing per-run limit and workflow/run
budgets. All reads retain policy, cancellation, timeouts and the 64-call and
900-second recipe ceilings.

One enabled exact match in the current bounded window permits the existing
ordinary action proposal. Multiple matches stop as `ambiguous_target`; no match
without usable continuation or within the search ceiling stops as
`target_not_observed`. Neither outcome proves whole-page absence or uniqueness.
Continuation is a series of live windows, not a frozen page or a global search.
No fuzzy matching, target substitution after approval, frame access or new
permission is introduced.

Use only the successful current observation's revision and action reference.
Expansion replaces prior references. Approval, live policy/grant checks and the
existing uncertain-effect rules remain unchanged. A stale or failed discovery
read cannot select a target from an older window. Once an action is dispatched,
its existing read-only reconciliation path cannot return to target discovery or
repeat the action. No public tool schema or deployed recipe changes.

## Verification

Begin with a composed regression where the target is in the second control
window and the workflow stops before approval. Cover empty windows, current
`after` versus cursor binding, malformed continuation, four-window exhaustion,
ambiguity, cancellation and changed generation/definition/deadline, plus stale
reads and uncertain actions. Real Chromium must discover controls after the
first 256 (including open shadow roots), preserve human approval and count one
server effect. Run the complete browser partition and relevant local sandbox
checks. Broader W4/W6 live-site acceptance remains separate.

Construction evidence in the local Linux ARM64 sandbox, 2026-11-11:

- Red: `make test-static
  PYTEST_ARGS=tests/unit/test_browser_workflow_target_discovery.py` failed because
  the workflow stopped before proposing approval for a target in its second
  window (expected one approval, observed zero). The regression now passes.
- The focused workflow suite passed all 83 cases, including 30 new discovery
  cases. Coverage includes current continuation binding, empty and ambiguous
  windows, malformed requests and state, persisted allowances and branch cursors,
  exhaustion, stale/unavailable reads and the existing sign-in wait.
- `make test-browser-cases
  CASES=tests/unit/test_browser_workflow_discovery_real_chromium.py` passed six
  cases. Real Chromium discovers targets beyond the first 256 controls, including
  an open shadow root, and reaches a fourth window without exceeding the cap.
  Server counters stay at zero before approval and at one after an approved
  action, including when its response is lost. Replacing an approved node with
  an identically labeled node produces zero effects and an unverified result.
- Broader checks passed 1,314 browser-related static tests and 170 browser
  contracts, plus lint, type checking, citation reconciliation and strict
  documentation checks. No public schema, deployed recipe, dependency, permission
  or milestone gate changed.

Final browser verification passed all 508 cases with no skips in 464.48 seconds,
using two workers and the established 64 MiB process stack. The full run emitted
no ASGI pending-request teardown diagnostic. These synthetic checks establish
construction coverage, not production or broader live-site acceptance.
