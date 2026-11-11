# ADR-0313: Verify browser completion rows across open shadow roots

- Status: Accepted under the owner's browser work-package continuation
- Date: 2026-11-11
- Related: ADR-0164, ADR-0311
- Detailed design: `docs/plan/browser-automation.md`

## Decision

Add `browser.observe` 1.10.0 and `browser.act` 1.9.0. Their row waits and
postconditions use ADR-0311's bounded main-document and open-shadow collection
reader. The trusted tool version selects scope, never a model argument.
Retained observe versions through 1.9.0 and act versions through 1.8.0 keep
main-document row checks and their published input/output schemas. Workflow
calls follow the tool versions pinned by their run, through the existing tool
pipeline. No recipe or selection behavior changes.

Row evidence must match the requested extraction scope as well as revision,
kind, index, columns and row limit. Cached evidence from a different scope is
not a match, including zero-timeout checks. A fresh provider response with a
mismatched scope fails closed through the existing extraction boundary. Older
services may refuse the widened request; there is no narrow-scope fallback.
The new action result schema admits version-2 extraction alongside its existing
saved-file result. No other public result or argument shape changes.

Fresh extraction, polling and the single stale-read recovery all use the same
scope. They retain the five-second/21-read ceilings, authentication interruption,
origin checks and cancellation behavior. An action is dispatched once; failed
verification after dispatch remains non-retryable and uncertain. A matching row
is observed evidence, not proof of causation or permission to repeat a write.
Hidden, editable, truncated and duplicate evidence cannot establish a unique
completion. Collection indexes remain current positions, not stable identities.

This W4 construction slice adds no authority, dependency, frame access, closed
root traversal or article extraction. No milestone, gate or engineering-plan
requirement changes. Broader live-site acceptance and production remain separate.

## Verification

Begin with regressions for a wrong-scope cached row falsely satisfying a
predicate and a delayed shadow-table receipt failing to complete after one
server effect. Cover version-selected scope, pinned schemas, stale reads,
conflicting/ambiguous evidence, refusal, interruption, cancellation and single
action dispatch. Run focused checks and the browser partition in the local Linux
sandbox, plus browser-related static/contract checks, lint, typing and docs.

Construction evidence in the local Linux ARM64 sandbox, 2026-11-11:

- Red: `make test-static
  PYTEST_ARGS="tests/unit/test_browser_completion_evidence.py -k different_extraction_scope"`
  failed because a document-only predicate accepted a shadow-scope row as
  `satisfied`. The added scope comparison makes it `not_observed`.
- Red: `make test-browser-cases
  CASES=tests/unit/test_browser_completion_real_chromium.py` failed the new
  delayed shadow-table receipt case (`not_observed` instead of `satisfied`),
  while eight existing cases passed. The new action version completes that
  fixture with exactly one recorded server effect.
- The expanded focused Chromium suite passed all 12 cases, including hidden and
  duplicate shadow receipts and retained document-only action behavior.
- The boundary suite covers all seven current/retained observe and act variants,
  output-schema validation, rejected model overrides, mismatched/unsupported
  providers, stale-read recovery, zero-timeout cached scope matching, conflicting
  failure evidence, authentication, cancellation and deadline cleanup. Missing
  evidence and failed reads never produce another action.
- All six retained tool specifications compare exactly to dev baseline
  `8a9e829b`, including their input/output schemas and descriptions. Composition
  selects the new versions and remains within the 6,000-token definition cap.
- All 1,250 browser-related static tests and 170 browser contracts passed;
  lint and type checking also passed. The diff-derived reading minimum is A.

The complete browser partition passed all 495 cases with no skips in 434.09
seconds, using two workers and the established 64 MiB process stack. Its teardown
also emitted an ASGI pending-request/`GeneratorExit` diagnostic without failing a
case; this is recorded separately from the passing test outcomes. The suite uses
synthetic sites and does not establish production or live-site acceptance.
A final isolated rerun of the changed completion module passed all 12 cases in
30.05 seconds without that teardown diagnostic. Citation reconciliation and
strict documentation checks passed; no citation content required adjustment.
