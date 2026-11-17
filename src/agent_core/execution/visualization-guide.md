# Offline artifacts, charts and diagrams

Use `artifact.read_guide` through `tool.call` to read the `build`, `charts` or
`diagrams` section. These fixed instructions need no approval or shell command.
Use observed or owner-supplied data, label sources and units, and explain missing
values and transformations. Never invent chart data with `image.generate`.

Use `artifact.build` through `tool.call` for one complete offline build. Supply
all UTF-8 input files, an argv command and explicit outputs together. The one
ordinary approval, when required, covers that exact package. Its fresh sandbox
has no network, credentials or tool bridge. Files are attached before cleanup;
never write inputs in a separate call or rely on a previous workspace.

Example package:

```json
{
  "files": {
    "spec.json": "{\"type\": \"bar\", \"title\": \"Example counts\", \"columns\": [\"Label\", \"Count\"], \"rows\": [[\"A\", 2], [\"B\", 3]], \"x\": \"Label\", \"y\": [\"Count\"], \"y_label\": \"Count\", \"source\": \"Illustrative input\"}",
    "render.py": "import json, shutil\nfrom pathlib import Path\nfrom agent_core.execution.visuals import render\nfor item in render(json.load(open('spec.json')), Path('/workspace')):\n    shutil.copyfile(item['path'], item['filename'])\n"
  },
  "command": [
    "python",
    "render.py"
  ],
  "outputs": [
    {
      "path": "chart.png",
      "filename": "chart.png",
      "media_type": "image/png"
    },
    {
      "path": "chart.csv",
      "filename": "chart.csv",
      "media_type": "text/csv"
    },
    {
      "path": "source.json",
      "filename": "source.json",
      "media_type": "application/json"
    }
  ]
}
```

The command runs from `/workspace`; every package path is relative to that root.
Inputs are at most 32 files and 1 MiB total. Request at most 16 outputs, each at
most 32 MiB. Command timeout defaults to 120 seconds, at most 240. A nonzero exit,
timeout or kill attaches nothing. An export failure lists any already attached
files. Do not claim completion unless `complete` is true. A changed package is a
new build and may require approval again; never repeat an uncertain build.

For standard charts and diagrams, put a JSON spec and a small Python wrapper in
`files`. The wrapper can call `agent_core.execution.visuals.render(spec,
Path('/workspace'))`, then copy the returned paths to the explicit output paths
in the package. The helper emits PNG, CSV (charts) or DOT (diagrams), and JSON
source; add `"formats": ["svg", "pdf"]` to request vector files. Include those
files in `outputs`. Read the `charts` or `diagrams` section for the spec format.
For custom infographics, Matplotlib and Graphviz are installed. Include the
underlying CSV/source and PNG preview, with explicit labels and units.

The lower-level command `python -m agent_core.execution.visuals SPEC.json` is
still available. It prints its output paths but does not attach them itself.
The build tool attaches only the explicitly requested outputs.

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
