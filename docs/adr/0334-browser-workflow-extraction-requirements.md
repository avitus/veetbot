# ADR-0334: Explicit evidence requirements for workflow extraction

- Status: Accepted under the owner's browser work-package continuation
- Date: 2026-11-18
- Related: ADR-0161, ADR-0164, ADR-0311, ADR-0328
- Detailed design: `docs/plan/browser-automation.md`

## Decision

A reviewed extraction step may declare `minimum_rows` (a strict integer from zero
through its `row_limit`) and `require_complete` (a strict boolean). Defaults of
zero and false preserve existing behavior and recipe identities. Both fields are
valid only for extraction steps and become part of the binding when non-default.
They are local acceptance requirements, not additional browser tool arguments.

The existing fresh read, pinned extraction request and schema-validity checks
still run. A valid result with fewer rows than required, or with omitted rows,
source/row scan limits or a byte limit when completeness is required, stops as
`insufficient_evidence`. It does not complete that step or propose later actions.
Invalid or missing extraction remains `unverified`. Both outcomes retain bounded
evidence and tool-free reporting; neither retries extraction or replays an earlier
effect. Existing budgets, authorization and uncertain-action handling remain.

Completeness describes only the selected visible collection under the pinned
observation scope. It does not prove that the website returned every historical
record, that virtualized or hidden rows exist or do not exist, that values are
true, or that rows are semantically distinct. Required fields still determine row
schema validity. Empty complete collections are valid when the declared minimum
is zero. No public tool schema, deployed recipe, permission or gate changes.

## Verification

Begin with failing recipe-contract and composed execution regressions. Cover
empty/short/sufficient collections, every explicit coverage bound, optional
partial results, invalid rows, closed field types and impossible thresholds,
legacy identity and changed bindings. Real Chromium must show an insufficient
collection cannot reach a later approval, sufficient evidence retains ordinary
approval, and earlier effects are never replayed. Broader acceptance and production
verification remain separate from these construction checks.

Construction evidence in the local Linux ARM64 sandbox:

- Red: `make test-static
  PYTEST_ARGS=tests/unit/test_browser_workflow_extraction_requirements.py`
  initially rejected both supported fields. After adding the closed contract,
  six execution cases failed because empty/short results and each coverage limit
  still reached a later approval. Those cases now stop without an approval or
  effect, while explicit empty/partial acceptance retains its behavior.
- The initial focused extraction/branch/readiness suite passed 67 tests.
- `make test-browser-cases
  CASES=tests/unit/test_browser_workflow_extraction_requirements_real_chromium.py`
  passed eight cases. Real document and open-shadow collections satisfy reviewed
  thresholds; short, empty and omitted-row cases stop before later effects.
  Earlier effects, including a lost response followed by read-only reconciliation,
  occur exactly once. Each case performs exactly one extraction.
- Broader verification passed 1,621 selected browser/steering static tests and
  173 contracts, plus lint and type checking. Closed types, impossible row
  thresholds, legacy identity and every reported coverage limit are covered.
  Reading lane A matches the diff-derived minimum.
- The full `make test-browser BROWSER_TEST_WORKERS=2` partition passed all 433
  cases in 506.95 seconds, with no skips or warnings. Citation reconciliation
  and strict documentation checks passed. No milestone or gate moves.
