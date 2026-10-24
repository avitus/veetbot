# Static charts and diagrams

Use the installed renderer for numerical charts and exact diagrams. Rendering
runs offline in this disposable workspace; it does not attach files. Use
`artifact.export` on the successful result's files before the run ends or pauses.
Never use `image.generate` to invent data points, labels or diagram connections.

1. Obtain the actual data from the owner or an authorized tool. State its source
   and units. Explain transformations; do not invent values or silently fill gaps.
2. Write a JSON specification with `workspace.write_text`.
3. Call `sandbox.run_command` with an argument vector:
   `["python", "-m", "agent_core.execution.visuals", "spec.json"]`.
4. Check both the process exit code and the JSON result. A success lists `files`,
   each with `path`, `filename`, `media_type` and `size_bytes`. Export each file
   using `artifact.export` with that path, filename and media type. The PNG is the
   preview; chart CSV includes the underlying data with text safety encoding;
   diagram DOT is editable.
   JSON preserves the complete specification. Only claim attachments after export
   succeeds. If an export fails, explain which file is missing.

The default output is PNG plus CSV (charts) or DOT (diagrams), and JSON source.
Add `"formats": ["svg", "pdf"]` for optional vector downloads. There is no need
to list PNG; it is always included. The Apple viewer previews PNG; SVG and PDF
are downloads. Each successful render uses a fresh directory and prints its
paths, so always use the returned paths rather than guessing filenames.
The input filename is relative to the command's working directory. Output paths
are always relative to `/workspace`, including when the command runs in a
subdirectory; pass them directly to `artifact.export`.

CSV text fields are quoted. Headers and text cells beginning with `=`, `+`, `-`,
or `@` (including full-width forms or after leading whitespace), or a line break,
receive a leading apostrophe to prevent formula interpretation on import.
Numbers, null cells, and row order are unchanged. Explain this CSV transformation;
`source.json` retains every original value for exact reuse, and plotted labels
remain unchanged. Spreadsheet applications can remove escaping when a CSV is
saved again; do not treat a re-saved CSV as trusted executable input.

## Charts

```json
{
  "type": "line",
  "title": "Monthly revenue and costs",
  "columns": ["Month", "Revenue", "Costs"],
  "rows": [["Jan", 120, 80], ["Feb", 150, 95], ["Mar", 180, 110]],
  "x": "Month",
  "y": ["Revenue", "Costs"],
  "x_label": "Month",
  "y_label": "USD thousands",
  "source": "Owner-supplied monthly figures",
  "formats": ["svg", "pdf"]
}
```

- `line`: trends in the supplied order. X values are all numbers or all strings.
  Use ISO dates as strings for evenly spaced periods. Irregular intervals need
  numeric elapsed time as X; do not imply equal spacing for irregular dates.
- `bar`: compare categories, one or more Y columns, at most fifty rows. X values
  must be unique. The vertical scale includes zero.
- `scatter`: numeric X and one or more numeric Y columns.
- `histogram`: one Y column of raw observations; omit X. Optional `bins` is an
  integer from 1 to 100 (default 20). Y axis defaults to Count. CSV contains the
  raw observations, not a fabricated bin table.
- `heatmap`: categorical X and one categorical Y column, plus `value` naming a
  numeric column. Each X/Y pair must be unique; aggregate explicitly beforehand
  if needed. Missing cells stay missing. At most fifty categories on each axis.
- `pie`: X labels and one Y column, nonnegative values with a positive total,
  at most fifty rows. Prefer bars when precise comparisons matter.

`title`, `columns`, `rows`, `type` and `y` are required. X is required except for
histograms. `x_label`, `y_label`, `source` and `formats` are optional. Column names
must be unique; every row has exactly the same number of cells as columns. Cells
are strings, finite numbers, or null. Null numeric cells are gaps/omitted marks
and remain empty in CSV; they are not converted to zero. Pie values cannot be
null. Every plotted series needs at least one number. Labels are literal text,
including dollar signs; no LaTeX or expressions run. The renderer does not sort,
aggregate, interpolate, retrieve URLs or infer units.

Limits: 1 MiB JSON, 10,000 rows, twelve columns, eight plotted series, 160-character
labels (source 240 characters). Break larger requests into explained subsets;
do not silently truncate. The PNG is 1500 by 900 pixels.

## Diagrams

```json
{
  "type": "diagram",
  "title": "Request approval",
  "direction": "LR",
  "nodes": [
    {"id": "request", "label": "Request", "shape": "ellipse"},
    {"id": "policy", "label": "Approval needed?", "shape": "diamond"},
    {"id": "execute", "label": "Execute"},
    {"id": "owner", "label": "Ask owner"}
  ],
  "edges": [
    {"from": "request", "to": "policy"},
    {"from": "policy", "to": "execute", "label": "No"},
    {"from": "policy", "to": "owner", "label": "Yes"}
  ],
  "formats": ["svg", "pdf"]
}
```

Use this for flowcharts, architecture, organization and relationship diagrams.
`direction` is `LR` (left to right, default) or `TB` (top to bottom). Shapes are
`box` (default), `ellipse`, or `diamond`. Node IDs must be unique; every edge
endpoint must name a node. Up to sixty nodes and 120 edges. Labels are plain
text; raw DOT attributes, HTML, images and external links are not accepted.
All edges are directed; describe uncertain or inferred relationships explicitly
in their labels. Render large systems as several focused diagrams for readability.
The generated DOT can be downloaded and edited in a Graphviz-compatible tool.

For a specialized visualization outside these shapes, Matplotlib and the `dot`
binary are installed for custom sandbox scripts under the same permissions.
Keep the PNG preview and underlying CSV/source, with explicit labels and units.
The helper's bounds are convenience limits, not a replacement for sandbox limits.
