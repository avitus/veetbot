# ADR-0161: Deterministic bounded browser extraction

- Status: Accepted under the owner's ADR-0156 implementation instruction
- Date: 2026-10-01
- Related: ADR-0058, ADR-0156, ADR-0157, ADR-0160
- Detailed design: `docs/plan/browser-automation.md`

## Decision

Version all three browser tools to 1.4.0. Add an optional `extract` request to
`browser.observe`, exclusive with wait and continuation. It supplies the current
`expected_revision`, a collection `kind` (table, list or form), its zero-based
visible `index` (0–15), one to eight uniquely named typed columns, and a row
limit (1–50, default 20). Each field has a simple identifier, column index
(0–15), primitive type (string, integer, number or boolean), and required flag.
No selectors, JavaScript, profile, URL, storage or attribute query is accepted.

This is a fresh read of the currently visible collection at that index, not a
stable identity assertion about a table that may have moved since the last
observation. Reject obsolete, foreign and navigated revisions before collection.
Return a fresh ordinary observation with an extraction result and new evidence
references. Existing action references are replaced as on every observation;
extraction references cannot authorize actions or act as expansion anchors.

The optional provider extraction capability runs through the existing hosted
observe endpoint, lease validation, session lock and principal/profile binding.
Unsupported adapters refuse explicitly. Extraction neither sends an action nor
advances its sequence. Cancellation, failed capture and navigation races use the
existing atomic observation cleanup; a failed read publishes no new references.

Scan at most 8,192 main-document nodes for the chosen collection, then at most
4,096 descendant nodes for rows. No frame or shadow-root traversal. Native
HTML tables/lists/forms and their corresponding explicit ARIA roles are
recognized. Table rows use visible cell order, including header rows and
excluding nested collections; spanning cells are reported as unsupported
structure rather than guessed.
Lists have one text column. Form columns are label, role, disabled, checked and
required, in that order; editable values and selected option values are excluded.

Each requested cell summary visits at most 256 nodes and retains at most 256
UTF-16 code units without cutting a surrogate pair. Hidden/ARIA-hidden subtrees,
scripts, styles and editable controls contribute no text. Native form labels
and explicit accessible names may identify controls, but never their values.
At most eight labels are resolved from 1,024 ID characters; referenced labels'
ancestor checks share the cell's 256-node budget. Hidden ancestors exclude
their labels, and clipped ID lists or exhausted budgets mark the cell truncated.
Report collection/row scan limits, requested row limit, known omitted rows and
cell truncation honestly; unknown content beyond a scan bound is not absence.

The deterministic schema converter accepts exact JSON-style numbers and only
`true` or `false` booleans. Integers must fit the exact JSON integer range
(plus or minus 9,007,199,254,740,991); numbers must be finite. Empty or absent
cells are missing. Invalid or truncated cells have null values and explicit
status, retaining bounded visible text as evidence. Required missing cells and
any invalid/truncated cells make a row schema-invalid. Optional missing cells
are explicitly missing without invalidating the row. Schema validity is not
truth, authenticated identity, business completion or authorization.

Cap the extraction result at 64 KiB by omitting whole trailing rows and counting
them. Canonical and model byte bounding also omit whole rows with explicit
counts; an extraction envelope that cannot fit is omitted as a whole. Never
clip typed records or silently fall back to a mechanical JSON excerpt. Prefer
requested extraction rows over optional controls, regions and general prose
while retaining revision and coverage; ordinary observations retain their
existing control priority. Artifact replay and the evidence digest preserve the
semantic result but ignore fresh evidence-reference bytes. All output is external-untrusted.

## Verification and limits

Require red-green tool/schema, hosted lease/unsupported, real Chromium table,
list/form, missing/invalid/truncated values, secret canary, scan/count/byte-bound,
stale/foreign revision, capture cancellation and model-request/replay coverage.
Run the required full sidecar gate with real Chromium and no silent skips.

This delivers the deterministic extraction part of W3. It does not interpret
currency/dates, read input values, infer merged grids, traverse frames, promise
stable collection identity, or measure live/model task success. No dependency,
new tool namespace, approval weakening or engineering-plan divergence is added.
