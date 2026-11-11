# ADR-0308: Discover semantic browser regions in open shadow roots

- Status: Accepted under the owner's browser work-package continuation on 2026-11-10
- Date: 2026-11-10
- Related: ADR-0156, ADR-0160, ADR-0162, ADR-0163
- Detailed design: `docs/plan/browser-automation.md`

## Decision

Extend W3 semantic-region discovery into open shadow roots belonging to the
current main document. Existing controls and readable text already traverse
these roots. Forms, dialogs, headings, status messages and alerts in web
components must also supply bounded region evidence and existing revision-bound
focus handles. This amends ADR-0160's exclusion of shadow regions.

Region coverage version 2 names `main_document_and_open_shadow`; version 1
with `main_document` remains accepted for older providers. Scan at most 8,192
nodes, retaining at most 32 regions in the existing priority order. Each
summary retains its 256-visit and 512-character bounds. Traverse iteratively,
shadow children before light children, without materializing whole subtrees.
Composed-ancestor checks for slotted nodes consume the same visit budget.
These are DOM-subtree observations, not a complete flattened accessibility
tree: a focused shadow region does not adopt assigned light nodes outside its
subtree. Bounds and summary truncation remain explicit.

Exclude hidden, ARIA-hidden, transparent and editable subtrees, including their
shadow descendants and light nodes assigned beneath excluded slot ancestors.
Frames and closed roots remain excluded. Focus revalidates the full composed
ancestor chain, including shadow hosts, and rejects detached or newly excluded
regions before capture. References remain read-only region references; actions
still require fresh control references and existing authorization. No new tool,
origin permission, extraction mode, credential access or automatic write is added.

Keep collection extraction's existing main-document-only scope. Preserve new
region coverage through hosted transport, canonical bounding and small-budget
model projection. Existing cleanup invalidates every handle on capture failure.

The release review identified a mixed-version cutover: the browser service starts
before application workers switch. Workers now advertise the exact version-2
coverage capability in an HTTP header. Unadvertised version-2 region evidence is
omitted from the wire response, never relabeled as document-only; ordinary page
text, controls and facts remain available. Version-1 coverage is unchanged. This
negotiation also preserves compatibility if application promotion fails, without
requiring a browser-service rollback to repair a schema mismatch. Canonical
snapshots retain their full evidence. JSON and progress-stream regressions cover
legacy, current and unknown capabilities against both coverage versions.

## Verification

Construction evidence, 2026-11-10, in the local Docker Desktop Linux VM:

- Red: `make test-browser-cases CASES=tests/unit/test_browser_shadow_regions_real_chromium.py`
  failed because the open-shadow form was absent from semantic regions. The
  first implementation made that regression pass.
- Focused region, shadow and focus tests: 29 passed. They cover nested roots,
  hidden/editable/closed/frame canaries, slotted-node exclusions, traversal and
  summary bounds, priority, changed-host focus, and non-actionable references.
- Projection, tools, readiness, progress, text and extraction unit tests:
  152 passed. Provider and hosted session contracts: 28 passed, including legacy
  coverage and version 2 transport.
- `make test-browser BROWSER_TEST_WORKERS=4`: 414 passed in 299.53 seconds.
  Includes a delayed shadow-region receipt with exactly one observed effect.
- `make test-static`: 10,014 passed and 3 skipped in 672.32 seconds.
- Lint and type checking passed; documentation and citation checks accompany
  the change.

The initial browser partition had 413 passes and one Chromium process crash in
the existing 9,000-deep DOM stress case, before region collection. The same crash
reproduced with the unchanged dev runtime. Increasing only the test container's
process stack to 64 MiB (`--ulimit stack=67108864:67108864`) made the isolated case
and complete browser partition pass; no test or production limit was weakened.
This is direct-dev construction evidence, not live-site or production acceptance.

Release-review verification, 2026-11-11: the new hosted negotiation regression
first failed in four legacy/unknown-capability cases while 26 existing cases
passed. After the repair, the local Linux browser-progress, download and email
reader atoms passed 139 tests; the hosted-client and service HTTP contract atoms
passed 62. The two coverage versions and JSON/streamed paths are covered without
changing the canonical snapshot. Commands used `circleci testsuite run` with
the relevant static and contract file patterns through the Linux sandbox.
