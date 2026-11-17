# ADR-0327: Region-scoped targets in reviewed browser workflows

- Status: Accepted under the owner's browser work-package continuation
- Date: 2026-11-14
- Related: ADR-0163, ADR-0164, ADR-0308, ADR-0315
- Detailed design: `docs/plan/browser-automation.md`

## Decision

A reviewed action step may declare `target_region` using the existing closed
region-evidence shape: `kind: region`, `region_kind` and complete normalized
`text`. Only action steps admit it. This is reviewed recipe data, not a selector,
page-authored instruction, cached reference or additional permission.

Before target discovery, issue an ordinary observation and find exactly one
matching, non-truncated region in that bounded result. No match stops as
`region_not_observed`; duplicates stop as `ambiguous_region`. Neither claims
whole-page absence or uniqueness. Queue an ordinary focused observation using
only that result's region reference and revision. The focused result and every
subsequent control window must retain focus metadata. Missing focus, failed reads,
foreign origins and interrupted observations cannot fall back to global controls.

The initial region read, focus read and any control continuations share ADR-0315's
four-window allowance per action. Persist each requested window before dispatch;
resumption and generation changes cannot reset the allowance. All reads retain
ordinary policy, cancellation, runtime budgets and the 64-call/900-second ceilings.
Only a unique enabled exact role/name target in the focused window may be proposed.
Approval and live revision checks remain unchanged. After dispatch, reconciliation
is read-only and cannot restart region discovery or replay the action.

Omitting `target_region` preserves legacy recipe digests and execution. A changed
region condition changes the definition binding. Existing open-shadow focus is
available through the pinned tool/provider contract; no frame or closed-root
authority is added. No public tool schema, deployed recipe, dependency, milestone
gate or engineering-plan requirement changes. This extends W3/W6 construction;
live-site acceptance remains separate.

## Verification

Begin with a rejected valid region-scoped recipe, then show that duplicate global
labels can reach one ordinary approval after a unique focused read. Cover missing,
duplicate and truncated region evidence, missing focus, discovery exhaustion,
fresh references, legacy digests and response loss. Real Chromium must distinguish
the intended form from a same-label control outside it, including open shadow
roots, and record exactly one approved server effect. Run the complete browser
partition and relevant sandbox checks before direct-dev delivery.

Construction evidence in the local Linux ARM64 sandbox:

- Red: `make test-static
  PYTEST_ARGS="tests/unit/test_browser_workflow_regions.py -k actions_accept"`
  rejected the valid closed region constraint. After adding the schema, the
  baseline runner failed `-k normal`: duplicate global labels produced no
  approval instead of one uniquely focused target. Both now pass.
- The focused workflow suite passed all 113 cases, including 21 new cases.
  These cover closed modes, normalized complete matching, absent/duplicate/
  truncated regions, stale reads, foreign origins, missing or lost focus,
  four-window exhaustion, current references and changed definition identity.
  Existing branch, steering, discovery and legacy digest checks also pass.
- `make test-browser-cases
  CASES=tests/unit/test_browser_workflow_regions_real_chromium.py` passed five
  cases. Ordinary and open-shadow forms select the intended control despite
  an identically labeled control outside them. Each approved task records one
  intended server effect, including response loss. Duplicate regions request no
  approval; replacing the approved control records zero effects and stops
  unverified. No outside control is clicked.

Broader construction checks passed 1,550 browser/steering static tests and 173
contracts, plus lint, type checking, citation reconciliation and strict
documentation checks. Reading lane A matches the diff-derived minimum. These
synthetic checks do not establish production or broader W3/W6 live-site acceptance.

Final schema-test review strengthened the non-action cases to begin with otherwise
valid steps and require the mixed-mode refusal. All 21 region cases, lint and
type checking passed again. The first full browser run passed 414 cases and timed
out in two existing moving-page navigation/sign-in fixtures. Both passed in an
isolated two-case rerun. With no production or timeout changes, the final complete
browser run passed all 416 cases with no skips in 457.69 seconds, using two workers
and the established 64 MiB process stack. That final run emitted no ASGI
pending-request or Playwright teardown diagnostic.
