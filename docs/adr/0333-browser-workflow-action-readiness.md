# ADR-0333: Readiness checks before reviewed browser actions

- Status: Accepted under the owner's browser work-package continuation
- Date: 2026-11-18
- Related: ADR-0159, ADR-0164, ADR-0315, ADR-0327, ADR-0328
- Detailed design: `docs/plan/browser-automation.md`

## Decision

A reviewed action may declare an optional `precondition` using the existing
bounded browser condition contract. Before discovering its target, the runner
issues an ordinary read-only `browser.observe(wait_for=...)`. Only a satisfied,
uninterrupted, same-origin result permits a separate fresh target read. Positive
declared failure stops as `observed_failure`; missing or ambiguous evidence and
ordinary read failures stop as `unverified`. Authentication interruptions retain
the existing verified sign-in continuation. No approval or action is proposed
without satisfied readiness.

The wait uses existing five-second/21-read bounds and spends the normal workflow
and run budgets. It does not count as completed work or provide a reusable action
reference. Target discovery retains its four-window bound and optional region
constraint. The eventual action retains current policy, approval, revision checks
and postcondition reconciliation; readiness never authorizes an effect. A lost
action response cannot restart readiness or repeat the action.

The pending readiness read is checkpointed through the ordinary tool pipeline.
On resumption, generation changes while still reading restart readiness with
fresh evidence. Pending actions from an old generation remain refused; an
ordinary approval resume does not repeat readiness or transfer the approved target.
Cancellation, steering and deadline checks remain at existing boundaries.
Pinned-capability preflight checks the additional wait arguments before selection.
Omitting the field preserves legacy recipe identities and behavior. The field is
valid only on action steps; no public tool schema or deployed recipe changes.

## Verification

Start with a failing recipe-contract regression. Cover readiness success, positive
failure, missing/conflicting evidence, target replacement, bounded waits, pinned
capabilities and legacy identity. Composed and real Chromium checks must prove
that only satisfied readiness reaches ordinary approval and exactly one effect,
and that response loss never replays the action. This is W4/W6 construction;
production and broader live-site acceptance remain separate.

Construction evidence in the local Linux ARM64 sandbox:

- Red: `make test-static PYTEST_ARGS=tests/unit/test_browser_workflow_readiness.py`
  initially rejected the supported readiness field. After adding the closed
  contract, four execution cases failed: the readiness wait was omitted and
  missing, failed or conflicting evidence still reached approval. The runner
  now requires satisfied readiness before fresh target discovery.
- The initial focused readiness/workflow/capability run passed all 44 tests.
- `make test-browser-cases
  CASES=tests/unit/test_browser_workflow_readiness_real_chromium.py` passed all
  six cases. A genuinely delayed page marker permits ordinary region targeting
  and one approved effect. Missing, failed and conflicting evidence produce no
  approvals or effects; a replacement target has no effect, and response loss
  is reconciled without repeating the action.
- Broader verification passed 1,593 selected browser/steering static tests and
  173 contracts, including readiness-enabled authentication resumption and
  no-replay cases. Lint and type checking passed. Reading lane A matches the
  diff-derived minimum.
- The full `make test-browser BROWSER_TEST_WORKERS=2` partition passed all 425
  cases in 573.26 seconds, with no skips or warnings. Citation reconciliation
  and strict documentation checks passed. No milestone or gate moves.
