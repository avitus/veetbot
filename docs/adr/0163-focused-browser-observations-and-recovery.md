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
