# ADR-0354: Verify expected browser extraction rows

- Status: Accepted under the owner's browser work-package continuation
- Date: 2026-11-25
- Related: ADR-0164, ADR-0313, ADR-0334, ADR-0350
- Detailed design: `docs/plan/browser-automation.md`

## Decision

A reviewed extraction step may declare `expected_row`, one to eight typed
fields with expected values using the existing `BrowserRowField` contract.
The field definitions must exactly match the step's extraction fields in order,
including names, columns, types and required flags. All must be required and
names must be unique. The collection kind, index and row limit come from the
step; no second extraction specification or model-authored selector is added.
Omitted or null expectations preserve legacy behavior and definition identity.
An expectation becomes part of the recipe binding when present.

After ordinary fresh extraction, schema checks and ADR-0334's row-count and
coverage requirements, the runner evaluates the existing typed-row predicate
against that result's own revision and pinned extraction scope. Exactly one
matching retained row permits the step to complete. Zero or multiple matching
rows stop as `insufficient_evidence`; invalid or missing extraction retains
`unverified`. A returned condition flag or earlier capture cannot supply proof.
No polling, additional capture, retry, branch or write is introduced. Subsequent
actions still resolve their own fresh target and require current authority.

The match describes only the returned visible collection. It is not a promise
of website truth, whole-site uniqueness, or persistence into the next action's
capture. Completeness remains an independent opt-in requirement. This does not
enable typed-row destination rechecks in later target windows under ADR-0350.
Existing budgets, cancellation, generation/definition binding and uncertain-write
rules remain. No public tool schema, deployed recipe, dependency, permission,
milestone or gate changes; no engineering-plan requirement is weakened.

## Verification

Begin with failing closed-contract and composed execution tests. Cover typed
values, mismatched schemas, legacy identity, unique/missing/duplicate matches,
invalid and partial evidence, current capture binding and ordinary approval.
Real Chromium must cover main-document and open-shadow rows, stale page values,
hidden/editable evidence and earlier writes (including response loss) occurring
exactly once. Run focused and broader browser tests, lint, typing, architecture,
reading-lane, citation reconciliation and strict documentation checks in the
local sandbox. Construction results are not production or live-site acceptance.

Construction evidence in the local Linux ARM64 sandbox:

- Red: `make test-static
  PYTEST_ARGS=tests/unit/test_browser_workflow_expected_row.py` initially failed
  11 cases because the expected-row field was not admitted. After adding its
  closed contract and preserving identity, four execution cases still reached
  approval for missing, empty or duplicate matches, or a forged condition flag.
  The local result check now stops those cases before any approval or effect.
- The initial focused extraction, capability and completion suite passed 114
  tests. Further cases verify a multi-column match and refuse to combine values
  from different rows. Legacy recipe digests remain unchanged.
- `make test-browser-cases
  CASES=tests/unit/test_browser_workflow_expected_row_real_chromium.py` passed
  all nine cases. Main-document and open-shadow matches retain ordinary approval;
  missing, duplicate, hidden, editable and changed evidence cannot advance.
  Earlier writes, including response loss reconciled by a read, happen once.
  Every case performs exactly one extraction.
- Final verification passed all 1,744 selected browser/steering static tests,
  171 contracts and the full 471-case real-browser partition, including all
  30 expected-row contract/execution cases and all nine new browser cases.
  Lint, type checking, architecture and reading-lane checks passed; lane A is
  both declared and required by the diff. Citation reconciliation and strict
  documentation checks passed without citation changes. Test runs emitted
  existing pytest/AnyIO deprecation warnings, with no skipped browser cases.
