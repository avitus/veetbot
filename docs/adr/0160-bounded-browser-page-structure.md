# ADR-0160: Bounded browser page structure

- Status: Accepted under the owner's ADR-0156 implementation instruction
- Date: 2026-10-01
- Related: ADR-0058, ADR-0156, ADR-0157, ADR-0159
- Detailed design: `docs/plan/browser-automation.md`

## Decision

Add optional semantic regions and explicit region coverage to observations from
all three browser tools, version 1.3.0. Region kinds are dialog, alert, status,
form and heading. Each carries a bounded visible-text summary, a truncation
flag, and an opaque reference bound to the enclosing observation revision.
These references identify evidence only: they are neither actionable controls
nor expansion anchors and acquire no handles or permissions. Admission rejects
regions without coverage, duplicate region references, and aliases of control
references. Existing control
ordering, continuation, live action guards and postconditions are unchanged.

The trusted collector visits at most 8,192 main-document nodes. It does not
enter frames or shadow roots. It selects at most 32 regions, prioritizing
visible dialogs, alerts, status messages, forms, then headings, with document
order within each kind. Each summary inspects at most 256 descendant nodes
and retains at most 512 characters. It excludes hidden subtrees, scripts,
styles, inputs, textareas, selects and editable content; it never reads form
values, storage, attributes containing credentials, or raw DOM into results.
Only visible text nodes contribute. Region text remains external-untrusted
website evidence and cannot authorize anything or prove a website effect.

Coverage version 1 reports main-document scope, scanned nodes, whether the scan
bound left nodes unread, and the number of matching regions omitted within the
scanned prefix. Unknown content beyond that bound is not counted as absent.
Summary truncation covers both text and descendant traversal limits. A region
collector failure participates in atomic observation cleanup; there is no
partially published new revision. The existing general readable-text field is
unchanged by this additive contract.

Canonical byte bounds include the serialized text-part wrapper and escaping.
Canonical output bounding and model projection preserve whole region records,
count omissions and include the coverage bounds. Model admission reserves room
for actionable controls before optional region summaries, and prioritizes
bounded region evidence over duplicated general prose. The canonical artifact
retains details omitted only by model admission. Region reference churn does
not count as task progress; changes to the observed semantic evidence do.
Hosted snapshots preserve the additive fields. Older adapters may omit them;
older clients continue to ignore unknown observation fields.

## Verification and limits

Require real Chromium fixtures for visible/hidden regions, input-value canaries,
priority, count/text/traversal bounds, changing evidence and expired references.
Verify small-budget projection, schema admission, replay and hosted transport.
Run the required browser partition and final sidecar aggregate.

This implements another bounded part of W3. It does not add task-aware control
ranking, structured table/form/list extraction, region pagination, frame access,
shadow-region coverage, or unchanged-region deltas. It introduces no dependency,
new tool, action authority, retention store or engineering-plan divergence.
