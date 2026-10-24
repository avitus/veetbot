# ADR-0243: Static charts and diagrams in the existing sandbox

- Status: Accepted (owner requested implementation, 2026-10-24)
- Date: 2026-10-24
- Related: ADR-0008, ADR-0029, ADR-0122, ADR-0140
- Detailed design: `docs/plan/sandbox-isolation.md`

## Decision

Extend the platform-owned Python sandbox image with pinned Matplotlib
dependencies, Graphviz and fonts. The base switches from Alpine to digest-pinned
Python 3.12 on Debian Bookworm so plotting wheels work on both amd64 and arm64;
the non-root numeric identity and all isolation restrictions stay unchanged.
Tini reaps orphaned command children so exited processes do not exhaust the
PID quota on the Debian image. The existing PID-limit regression stays unchanged.
A bundled, offline rendering helper accepts
bounded JSON data and emits a PNG preview, optional SVG and PDF downloads, and
the original chart rows as CSV. Diagrams accept named nodes and directed edges
for flowcharts, architecture and relationship graphs, and include editable DOT
source. This is an owner-authorized extension of existing artifact generation;
it introduces no milestone, tool, permission, provider or activation gate.

The helper runs only through `sandbox.run_command`. It never runs in the API,
worker or execution-service control plane. It preserves the existing sandbox
network denial, resource limits, credential exclusion and approval rules. Input
validation rejects invalid numbers, inconsistent rows, ambiguous heatmap cells,
unknown diagram endpoints and excessive sizes before rendering. Labels are text,
not executable expressions, HTML, URLs or Graphviz attributes. Outputs become
visible together only after every requested format succeeds. A retry creates
another workspace output directory instead of overwriting an existing result.

The agent uses observed or owner-supplied data, explains transformations and
missing values, and never uses image generation to fabricate a numerical chart.
The default instructions and command-tool description identify the helper and
its bundled guide. Existing pinned agent instructions remain pinned; the current
command tool can still describe the available image capability.
The command description stays compact; the JSON format lives in the guide so
the selected-browser tool catalog retains its existing 6,000-token ceiling.

Files reach the owner through separate `artifact.export` calls under ADR-0122.
The existing Apple artifact viewer previews PNG and downloads every format;
SVG/PDF do not need active-content rendering in the app. A chart always includes
CSV and PNG; vector formats are produced only when requested. Diagrams include
PNG and DOT, with the same optional vector formats. The machine-readable source
specification accompanies both, so the render can be reproduced.

## Verification

On 2026-10-24, the real Docker sandbox passed all 23 cases in
`tests/security/test_static_visuals_runtime.py` and
`tests/security/test_sandbox_runtime_m6.py` (56.97 seconds). They cover renderer
availability, actual plotted values and categorical positions, literal labels,
every format, original CSV rows, graph topology, retries, validation failures,
artifact byte fidelity and tenant separation, plus the existing isolation and
resource-limit regressions. The original image first failed the new capability
probe with both Matplotlib and Graphviz absent. The unchanged PID-limit test
reproduced the Debian zombie-process failure before Tini made it pass.

The 38 pure validation/cleanup cases in `tests/unit/test_static_visuals.py`
passed. A chart and diagram downloaded through the artifact path were visually
inspected and accepted by the exact `ArtifactImageDecoder` from the Apple client
(1500 by 900 and 378 by 167 pixels). This is decoder evidence, not a full native
UI or production gVisor run. The repository's final sidecar `make check` remains
required before delivery. No production activation or milestone completion is
claimed here.

## Consequences

The sandbox image becomes larger. Its Python dependencies are separately locked
and are not added to the worker's dependencies. Static previews need no external
service or interactive JavaScript. Arbitrary interactive charts, Mermaid syntax,
and a dedicated chart tool API remain outside this extension.
