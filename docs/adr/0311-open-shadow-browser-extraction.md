# ADR-0311: Extract bounded collections from open shadow roots

- Status: Accepted under the owner's browser work-package continuation
- Date: 2026-11-11
- Related: ADR-0156, ADR-0161, ADR-0308
- Detailed design: `docs/plan/browser-automation.md`

## Decision

Add `browser.observe` version 1.9.0. Its table/list/form extraction includes
open shadow roots in the main document. Versions 1.7.0 and 1.8.0 retain their
document-only extraction, and article extraction remains document-only in every
version. No new model argument selects a scope or grants authority. The tool
sets the trusted extraction request's scope; older requests default to the
existing document scope and omit that field on the hosted wire.

The widened result uses version 2 and `main_document_and_open_shadow`; version 1
retains the two existing document scopes. Providers must return the requested
scope, never silently substitute a narrower scan. A newer request to an older
service may refuse; it cannot claim complete shadow coverage. Pinned older
workers and tools keep their original request and response contracts.
Published input and output schemas remain unchanged for pinned versions; only
observe 1.9.0 advertises the widened extraction result.

Walk iteratively, shadow children before light children, within the existing
8,192 collection, 4,096 row and 256 cell visit ceilings. Composed-ancestor checks
consume those same budgets. Exclude hidden, transparent and editable hosts and
slot ancestors before collecting their content. A form retains an editor's own
label/role metadata but never descends into its value subtree. Resolve ID-based labels in
the control's own root; referenced labels share the cell budget with ancestor
checks. Never read entered values. Cells retain the 256 UTF-16-unit bound.

This is DOM-subtree extraction, not a flattened accessibility tree. Assigned
light nodes outside a shadow collection's subtree are not adopted as rows.
Closed roots, frames and nested collections stay excluded. Indexes identify the
currently visible collection in the requested scope, not a stable collection
identity. Existing schema validity, missing/truncated evidence, whole-row output
bounds, revision checks, cleanup and read-only evidence references remain.

No new dependency, write capability, origin permission or plan divergence is
introduced. This is W3 construction; live-site quality and production acceptance
remain separate.

## Verification

Begin with a real Chromium regression in which a shadow table is not found.
Cover nested roots, collection kinds, hidden/editable/slot canaries, scoped labels,
traversal limits, unsupported spans, pinned versions, provider scope mismatches,
hosted transport and bounded model projection. Run the complete browser partition,
browser-related static tests and contracts, lint, typing and documentation checks.

Construction evidence in the local Linux ARM64 container, 2026-11-11:

- Red: `make test-browser-cases
  CASES=tests/unit/test_browser_shadow_extraction_real_chromium.py` initially
  failed because a visible shadow table returned `not_found`. The first
  implementation made it pass.
- A second red regression exposed an editor's shadow descendants as extra form
  rows; the reader now stops at the editor. All 52 focused Chromium collection
  and article cases pass, including 25 new shadow cases and both pinned/current
  stale-reference and non-actionable-evidence checks.
- Pinned-schema regressions first failed on widened output enums. The retained
  extraction input and output schemas now compare equal to the previous dev
  revision, `715fe9a5`, in the Linux sandbox. Only observe 1.9.0 advertises the
  widened result. The existing 6,000-token tool-definition cap remains enforced.
- Boundary tests cover version-selected scope, rejected model scope overrides,
  mismatched providers, version/scope consistency, unchanged article scope,
  legacy/current hosted payloads and JSON/streamed service responses. Projection
  tests retain complete rows and explicit omissions under both scope versions.

Final verification: 1,213 browser-related static tests and 170 browser contracts
passed, together with lint, type checking, citation reconciliation and strict
documentation checks. The complete browser partition passed all 491 cases with
no skips in 453.03 seconds, using two workers and the established 64 MiB process
stack. An earlier concurrent browser attempt was stopped when the 8 GiB Docker
VM became resource-constrained; the completed run used reduced concurrency with
no test or production limit changes. This is construction evidence, not a live
site benchmark or production release.
