# ADR-0328: Check pinned capabilities before selecting a browser workflow

- Status: Accepted under the owner's browser work-package continuation
- Date: 2026-11-15
- Related: ADR-0164, ADR-0176, ADR-0314, ADR-0327
- Detailed design: `docs/plan/browser-automation.md`

## Decision

Select a reviewed workflow only when its required tool names are available in
the run's pinned advertised/deferred set and its required argument shapes validate
against those pinned input schemas. Check the single unambiguous intent/origin
match before creating workflow state, navigation or any other invocation. An
unsupported recipe returns to ordinary agent planning; it creates no partial
workflow, approval, tool usage or report-only restriction. Capability differences
cannot resolve ambiguous owner intent by choosing between matching recipes.

Use the existing local JSON Schema argument validator, reviewed literal values,
and non-executed placeholders for fresh revisions and references. Check navigation,
plain observation, observe conditions, action values and postconditions, read-only
action reconciliation, region focus, and typed extraction as applicable. Check all
declared steps, including forward branch destinations, without predicting the
branch. Feed collection checks its existing article extraction and bounded scroll
shapes rather than inspecting one schema enum. Optional discovery continuations
remain runtime capabilities; a missing continuation still stops discovery safely.

This is a bounded compatibility check, not a dry run or authorization decision.
It never calls a provider, learns a reference, evaluates page content, changes
tool pins, refreshes a catalog, or invokes policy/approval. Normal validation,
current authorization, fresh evidence, budgets and non-replay rules still govern
every actual call. Once a workflow has started, its existing binding and resume
rules apply; a capability change cannot silently discard uncertain work or switch
to another recipe. No public tool schema, dependency, deployed recipe, milestone
gate or engineering-plan requirement changes. This continues W6 construction;
production and broader live-site acceptance remain separate.

## Verification

Start with a composed regression where a matching recipe navigates before
discovering a missing action tool; require ordinary planning with no navigation.
Cover missing and deferred names, missing pinned specs, legacy argument modes,
region focus, postconditions, extraction fields, branches, feed capabilities,
ambiguous matching and unchanged recipe identity. Real Chromium must record zero
requests/effects for incompatible recipes and one normally approved effect for a
compatible recipe. Run focused, full browser and risk-relevant sandbox checks.

Construction evidence in the local Linux ARM64 sandbox:

- Red: `make test-static
  PYTEST_ARGS=tests/unit/test_browser_workflow_capabilities.py` failed all three
  initial cases. Missing action/observation tools allowed navigation before
  failure; a missing navigation tool failed the run instead of ordinary planning.
  The cases now pass without browser calls, approvals or workflow state.
- The focused workflow suite passed 131 cases, including pinned legacy modes,
  deferred tools, ambiguous intent, branches, region focus, feed collection,
  missing definitions and navigation-only/wait-only recipes. Preflight checks
  required shapes only; a wait-only recipe need not support an unused plain read.
- `make test-browser-cases
  CASES=tests/unit/test_browser_workflow_capabilities_real_chromium.py` passed
  three cases. Incompatible recipes record zero page requests, effects and tool
  calls. A compatible recipe records one page request, one ordinary approval
  and exactly one server effect.

Broader checks passed 1,569 browser/steering static tests and 173 contracts,
including two further cases proving that extraction field types and row limits
are validated, plus lint, type checking, citation reconciliation and strict
documentation checks. Reading lane A matches the diff-derived minimum.

The full `make test-browser BROWSER_TEST_WORKERS=1` partition passed all 419
cases in 895.49 seconds, with no skips or warnings. One worker limited contention
with other local sandbox work. This is construction evidence, not production or
live-site acceptance.

The PR 255 promotion repair passes the existing argument validator into the runner
from bootstrap instead of importing `tools.validation` from runtime. This restores
the Section 5 dependency boundary without changing capability or approval decisions.
The existing `tests/gates/test_import_boundaries.py` gate first failed on that exact
runtime-to-tools edge. The added injected-validator regression in
`tests/unit/test_browser_workflow_capabilities.py` first failed because preflight
did not accept a validator. Both failures were reproduced with
`scripts/run_in_linux_container.sh uv run pytest -q` and their respective test paths.
After the repair, the architecture and non-browser workflow selection passed all
158 cases (35 browser cases deselected) in 74.99 seconds, and the real Chromium
capability suite passed all three cases in 20.10 seconds. Repository lint and type
checking passed. No boundary rule, public schema, milestone or gate changes.
