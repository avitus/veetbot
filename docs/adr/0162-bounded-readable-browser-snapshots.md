# ADR-0162: Bounded readable browser snapshots

- Status: Accepted under the owner's ADR-0156 implementation instruction
- Date: 2026-10-01
- Related: ADR-0058, ADR-0156, ADR-0157, ADR-0160, ADR-0161
- Detailed design: `docs/plan/browser-automation.md`

## Reference and decision

The owner directed continued implementation using Stagehand as a reference,
with comparative benchmarks deferred. We inspected Stagehand at commit
`c88a64f8d48d13e5e4d43d14fd9e54e388de3383`: its
[observation service](https://github.com/browserbase/stagehand/blob/c88a64f8d48d13e5e4d43d14fd9e54e388de3383/packages/extension/services/observeService.ts)
captures a structured snapshot before inference, and its
[accessibility pruning](https://github.com/browserbase/stagehand/blob/c88a64f8d48d13e5e4d43d14fd9e54e388de3383/packages/extension/understudy/a11y/snapshot/a11yTree.ts)
and [tree formatting](https://github.com/browserbase/stagehand/blob/c88a64f8d48d13e5e4d43d14fd9e54e388de3383/packages/extension/understudy/a11y/snapshot/treeFormatUtils.ts)
retain useful content while removing structural noise. Apply that separation
to Veetbot's remaining whole-body `inner_text` read, with our own bounded DOM
collector. No Stagehand code, SDK dependency, cloud service or model call is added.

Browser tools version 1.5.0 add optional `text_coverage`. Read visible text in
the main document and open shadow roots, with shadow children before light
children, matching the existing control traversal. Frames and closed roots
remain excluded. Do not materialize the whole page's rendered text and then
truncate it. Visit at most 8,192 nodes, inspect at most 262,144 UTF-16 text units,
and return at most 256 KiB of UTF-8 text. Walk iteratively; do not enumerate all
children or recurse through a hostile deeply nested tree. Bound collection
before browser-to-provider transfer, with no DOM or private browser state in
the result. Composed-ancestor checks for slotted nodes share the same node-visit
budget; visits need not refer to distinct nodes.

Discard scripts, styles, templates, embedded documents, hidden/ARIA-hidden
subtrees, invisible text and editable content. Inputs, textareas, selects and
custom textbox/searchbox/combobox/spinbutton content supply no values. Visible
surrounding labels remain readable. Normalize whitespace and preserve block,
line-break and table-cell boundaries; adjacent inline text must not acquire
invented spaces. Text nodes contribute once, including slotted text. Read no
attributes as prose and never split a Unicode surrogate pair or UTF-8 sequence.
This is readable evidence, not a full accessibility tree or faithful CSS layout.

Integration with ADR-0148 preserves continuation of the agent's own approved
rich-text draft without collecting editable values. The runtime may retain one
already-submitted value and a weak node binding. A bounded live equality check
returns only a boolean; while the visible non-credential editor still matches,
the observation may label and echo the known submitted value within its existing
text ceiling. The check refuses editors above 256 nodes or 4,096 text units.
A changed, hidden, credential or oversized editor invalidates the receipt.
Unknown draft text never enters the snapshot and this is not publication proof.

Coverage version 1 reports scope, scanned nodes, inspected UTF-16 units,
node/text limit flags and known text bytes omitted by canonical output bounding.
A hit limit means unobserved content may remain; it is not a count of absent
content. Typed observations carrying this coverage reject a captured-text byte total
(including known omissions) above 256 KiB. Collection and canonical truncation are separate from the existing
model-projection omission count. Preserve provider text coverage through hosted
transport, artifact/replay and model admission; if essential coverage cannot
fit, omit the observation explicitly. Legacy providers may omit the new field.
The model view always names text/region scope and version but omits default false
flags, null cursors and zero text-omission counts from the provider coverage
records. These retain their typed defaults when absent; all non-default coverage
survives. This lossless compact form preserves room for
controls at the existing 1 KiB task budgets. Canonical output retains full fields.
For observations supporting continuation, reserve the first control that can fit
without optional location/title context before admitting that context, regions
or prose. Fitting only a later control
must not hide a discoverable earlier control and block prefix continuation.
An individually oversized control can still be skipped with explicit coverage.

Capture failures, cancellation and document changes use the existing atomic
observation cleanup. Control order, continuation, live action guards, approval
and grant consumption are unchanged. All collected text is external-untrusted.

## Verification and limits

Begin with real Chromium regressions for editable/hidden text and missing shadow
content, then verify block and inline boundaries, slots, Unicode, node/text/byte
bounds, hostile depth, cancellation and no whole-body `inner_text` invocation.
Verify typed coverage, canonical/model budgets, hosted transport and durable
replay. Run the existing mandatory browser partition and final sidecar gate;
do not create comparative benchmarks or claim measured task-quality gains.

This completes another W3 observation increment. Task-directed selection,
region/text pagination, frame access, action reuse and broader recovery remain
separate work. No engineering-plan or authorization requirement is weakened.
