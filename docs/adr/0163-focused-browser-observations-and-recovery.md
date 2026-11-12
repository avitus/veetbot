# ADR-0163 — Focused browser observations and bounded recovery

- Status: Accepted; amends ADR-0162 model projection
- Date: 2026-10-06
- Scope: The owner's explicitly authorized continuation of ADR-0156

## Context

Whole-page controls and prose can hide a task's form behind navigation. The
owner requested focused observations, region expansion, realistic small-budget
workflows, bounded recovery, and authentication interruptions. Stagehand is a
reference, not a dependency: its scoped accessibility capture resolves a focus
before collecting evidence. Our public interface retains opaque references,
trusted Playwright, origin isolation and revision-bound authority.

## Decision

1. Extend read-only observation expansion with `region_ref`, `expected_revision`
   and optional `text_offset` (UTF-8 bytes). Exactly one expansion mode is valid.
   Regions are captured as private stable element handles; a region reference
   is never an action target. Main and section landmarks join semantic evidence.
2. Focusing reads controls and bounded text inside that region, including open
   shadow text. Control continuations retain that scope. An ordinary observation,
   navigation or action resets focus. Active dialogs precede page navigation in
   default control discovery. All existing candidate, node and byte bounds hold.
3. Focus metadata reports its current reference, text offset and captured byte
   count and an opaque text cursor. Canonical and model projections offer
   `next_text` using that cursor and the bytes actually admitted. At small model
   budgets, retain scope/version, limit flags and omission counts, but mark scan
   counters as `diagnostics_in_artifact`; canonical evidence retains every count.
   Only `next_observe` exposes the eligible control continuation, avoiding a
   duplicate provider cursor that could skip the inline control prefix.
   This explicitly amends ADR-0162's lossless counter projection. A focused model
   view names its scope and keeps the full range metadata in the artifact.
   Reserve a compact, explicitly clipped region label so focus is discoverable. Text continuation verifies the current revision,
   stable
   region and unchanged bounded text; it never silently skips unseen prose.
   A collector ceiling remains an explicit limitation, not a completeness claim.
4. Recovery performs at most one fresh read after a stale observation failure,
   with at most three automatic recovery reads per run.
   It never resends an action or navigation, invents a replacement target, or
   retries an uncertain effect. Recovery evidence is separate from the failed
   operation and remains external-untrusted. Each read is a separate audited,
   policy-checked invocation, bounded by its timeout, cancellation, run deadline
   and remaining tool-call budget. The existing checkpoint event carries an
   optional runtime-call receipt, atomically with the pending checkpoint, so
   subsequent history reconstructs each call/result pair without inventing a
   model response or adding an event type.
5. Bounded main-document password, OTP, CAPTCHA-frame and sign-in prompt
   detection interrupts automation with a stable refusal and
   discards actionable references. The owner uses the existing device sign-in
   flow; the model never receives or enters credentials, MFA or CAPTCHA answers.
   A run can request sign-in at most twice. Its completed failed operation is
   retained before suspension; resume does not replay it. A challenge reached
   after a dispatched action returns an interruption observation without controls;
   the tool records it as outcome-unknown with a fixed sign-in marker, then the
   runtime may suspend. This never confirms the external effect. Failure to inspect an already
   dispatched action remains outcome-unknown. Existing profile
   generation, lease and grant validation require fresh evidence and authority.

## Reference

[Stagehand focus selector resolution, revision c88a64f](https://github.com/browserbase/stagehand/blob/c88a64f8d48d13e5e4d43d14fd9e54e388de3383/packages/extension/understudy/a11y/snapshot/focusSelectors.ts).
No SDK, cloud provider, model calls, arbitrary selectors or frame access are added.
Benchmarks remain deferred. These are regression workflows, not comparative scores.

## Open-shadow dialog follow-up — 2026-11-10

Decision 2's dialog priority now follows open shadow roots, including dialogs
that host their own shadow content. It shares the semantic-region collector's
iterative traversal and exclusion checks, with 8,192 discovery visits including
composed ancestors. Focus starts inside the selected root's own shadow content
before its light children. Control limits, revision and continuation fences,
frame exclusions and action authority are unchanged. This repairs the existing
W3 observation contract; it does not introduce a new capability or ADR decision.

Construction evidence from real Chromium in the local Linux ARM64 container,
using the previously established 64 MiB process-stack setting:

- Red: `make test-browser-cases
  CASES=tests/unit/test_browser_focus_real_chromium.py::test_shadow_dialog_priority_survives_projection_and_control_continuation`
  failed in both initial shadow shapes: `Navigation 0` appeared before `Task 0`.
- Extending that regression to focus reproduced an empty control list for a
  dialog hosting its own shadow root. Both defects then passed, with four shapes
  covering native dialogs, nested roots, shadow hosts and mixed slotted content.
- Assertions cover complete ordered continuation without duplicates, 1,024- and
  4,096-byte model projections, excluded hosts/slot ancestors, bounded priority
  discovery, scoped focus and rejection after dialog ordering changes.
- Full browser partition: 425 passed in 315.14 seconds. No skipped cases.
- After integrating dev's unrelated recall-context change, all 36 focus and
  shadow-region cases passed again. The four dialog-shape cases also passed
  the final focused-continuation assertion, including slotted light children.

This is direct-dev construction evidence. Live-site acceptance, production
delivery, comparative benchmarks and the broader W3 exit gate remain separate.

## Open-shadow authentication interruption — 2026-11-11

Decision 5's negative challenge detector now traverses the main document and
open shadow roots. Node and composed-ancestor visits share the 8,192-visit bound;
hidden hosts and slot ancestors exclude their content. Prompt text is limited
to 512 UTF-16 units in visible, noneditable form/dialog/button contexts, including
their ARIA roles. No credential or editable value is collected. Password and
OTP metadata still interrupts even beneath a passive reCAPTCHA badge, while the
badge's ordinary CAPTCHA metadata does not. Closed roots remain excluded and
ADR-0288's separate frame-credential guard is unchanged.

This extends detection within the existing observed scope, without adding
authentication authority or changing positive protected-state verification.
No match or scan exhaustion is still only bounded negative evidence. Detection
before dispatch refuses the action and invalidates references; detection after
dispatch returns the existing empty interruption observation, preserving the
uncertain-effect handling rather than replaying the action.

Construction evidence, 2026-11-11, in the local Linux ARM64 browser container
with the established 64 MiB process stack:

- Red: `make test-browser-cases
  CASES=tests/unit/test_browser_authentication_shadow_real_chromium.py` failed
  because a visible shadow password field did not raise `needs_user` before
  the proposed action. The first implementation made that regression pass.
- The initial focused authentication/focus run passed 49 cases. The final
  browser partition passed all 463 cases in 325.34 seconds, with no skips.
  Its 24 new authentication cases cover nested roots, composed prompt context,
  hidden/slotted ancestors, badge exceptions, unreadable credential/editable
  value sentinels, scan limits, reference cleanup, and one post-dispatch server
  effect followed by an empty interruption observation.
- Browser-related static tests: 1,167 passed. Browser contracts: 170 passed.
  Lint and type checking passed; documentation and citation checks accompany
  this construction evidence.

Direct-dev delivery does not establish live-site authentication acceptance or
production availability. Positive account/protected-state verification and
the broader W5 exit evidence remain separate.
