"""Offline static rendering. Invoked inside the sandbox, never by the worker.

Only validation uses the standard library. Matplotlib is installed in the
sandbox image, not in the application's environment.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

MAX_INPUT_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
CHART_TYPES = frozenset({"line", "bar", "scatter", "histogram", "heatmap", "pie"})
MEDIA_TYPES = {
    "png": "image/png",
    "svg": "image/svg+xml",
    "pdf": "application/pdf",
    "csv": "text/csv",
    "dot": "text/vnd.graphviz",
    "json": "application/json",
}
COLORS = ("#147D82", "#D08A32", "#6555A3", "#BF5962", "#527A42", "#467FA4", "#A16D4C", "#757575")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _text(value: object, *, maximum: int = 160, empty: bool = False) -> bool:
    return (
        isinstance(value, str)
        and (empty or bool(value.strip()))
        and len(value) <= maximum
        and all(ord(char) >= 32 or char == "\n" for char in value)
    )


def _number(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and -1e100 <= value <= 1e100
        and math.isfinite(value)
    )


def _keys(value: dict[str, Any], allowed: set[str]) -> None:
    _require(not set(value) - allowed, "Unknown specification field.")


def validate_spec(value: object) -> dict[str, Any]:
    """Reject ambiguous or excessive data before creating any output."""
    _require(isinstance(value, dict), "The specification must be a JSON object.")
    assert isinstance(value, dict)
    spec: dict[str, Any] = value
    kind = spec.get("type")
    _require(isinstance(kind, str) and kind in CHART_TYPES | {"diagram"}, "Unsupported type.")
    _require(_text(spec.get("title")), "A title of 1-160 characters is required.")
    _require(_text(spec.get("source", ""), maximum=240, empty=True), "Invalid source label.")
    formats = spec.get("formats", [])
    _require(
        isinstance(formats, list)
        and len(formats) <= 2
        and all(isinstance(item, str) and item in {"svg", "pdf"} for item in formats)
        and len(set(formats)) == len(formats),
        "formats accepts svg and pdf once each; PNG is always included.",
    )
    if kind == "diagram":
        _validate_diagram(spec)
    else:
        _validate_chart(spec)
    return spec


def _validate_chart(spec: dict[str, Any]) -> None:
    _keys(
        spec,
        {
            "type",
            "title",
            "source",
            "formats",
            "columns",
            "rows",
            "x",
            "y",
            "x_label",
            "y_label",
            "value",
            "bins",
        },
    )
    kind = spec["type"]
    columns, rows, ys = spec.get("columns"), spec.get("rows"), spec.get("y")
    _require(
        isinstance(columns, list)
        and 1 <= len(columns) <= 12
        and all(_text(name) for name in columns)
        and len(set(columns)) == len(columns),
        "Supply 1-12 unique column names.",
    )
    _require(isinstance(rows, list) and 1 <= len(rows) <= 10000, "Supply 1-10000 rows.")
    assert isinstance(columns, list) and isinstance(rows, list)
    for row in rows:
        _require(isinstance(row, list) and len(row) == len(columns), "Rows must match columns.")
        _require(
            all(cell is None or _number(cell) or _text(cell, empty=True) for cell in row),
            "Cells must be bounded text, finite numbers, or null.",
        )
    _require(
        isinstance(ys, list)
        and 1 <= len(ys) <= 8
        and all(isinstance(y, str) and y in columns for y in ys)
        and len(set(ys)) == len(ys),
        "y must name 1-8 distinct columns.",
    )
    assert isinstance(ys, list)
    for label in ("x_label", "y_label"):
        _require(_text(spec.get(label, ""), empty=True), "Invalid axis label.")
    _require("value" not in spec or kind == "heatmap", "value is only for heatmaps.")
    _require("bins" not in spec or kind == "histogram", "bins is only for histograms.")
    if kind == "histogram":
        _require("x" not in spec, "Histograms use one Y column of raw observations; omit x.")
        _require(
            type(spec.get("bins", 20)) is int and 1 <= spec.get("bins", 20) <= 100,
            "bins must be an integer from 1 to 100.",
        )
    else:
        _require(isinstance(spec.get("x"), str) and spec["x"] in columns, "x must name a column.")
        xs = [row[columns.index(spec["x"])] for row in rows]
        _require(
            all(_number(x) for x in xs) or all(_text(x) for x in xs),
            "X values must be all numbers or all nonempty text.",
        )
        if kind == "scatter":
            _require(all(_number(x) for x in xs), "Scatter X values must be numeric.")
        if kind in {"bar", "pie"}:
            _require(
                len(rows) <= 50 and len(set(xs)) == len(xs),
                "Bar and pie charts need at most fifty unique X values.",
            )
    if kind in {"histogram", "heatmap", "pie"}:
        _require(len(ys) == 1, "This chart type requires exactly one Y column.")
    if kind == "heatmap":
        _require(
            isinstance(spec.get("value"), str) and spec["value"] in columns,
            "A heatmap needs a numeric value column.",
        )
        categories = [row[columns.index(ys[0])] for row in rows]
        _require(all(_text(y) or _number(y) for y in categories), "Invalid heatmap Y category.")
        _require(
            all(_number(y) for y in categories) or all(_text(y) for y in categories),
            "Heatmap Y categories must have one type.",
        )
        _require(len(set(xs)) <= 50 and len(set(categories)) <= 50, "Heatmap axes exceed fifty.")
        _require(
            len(set(zip(xs, categories, strict=True))) == len(rows),
            "Duplicate heatmap cells require explicit aggregation.",
        )
        numeric_columns = [spec["value"]]
    else:
        numeric_columns = ys
    for column in numeric_columns:
        values = [row[columns.index(column)] for row in rows]
        _require(
            all(v is None or _number(v) for v in values) and any(_number(v) for v in values),
            "Every plotted series needs finite numbers; missing cells must be null.",
        )
        if kind == "pie":
            _require(
                all(_number(v) and v >= 0 for v in values) and sum(values) > 0,
                "Pie values must be nonnegative, non-null and have a positive total.",
            )


def _validate_diagram(spec: dict[str, Any]) -> None:
    _keys(spec, {"type", "title", "source", "formats", "direction", "nodes", "edges"})
    _require(spec.get("direction", "LR") in ("LR", "TB"), "direction must be LR or TB.")
    nodes, edges = spec.get("nodes"), spec.get("edges")
    _require(isinstance(nodes, list) and 1 <= len(nodes) <= 60, "Supply 1-60 nodes.")
    _require(isinstance(edges, list) and len(edges) <= 120, "Supply at most 120 edges.")
    assert isinstance(nodes, list) and isinstance(edges, list)
    identifiers: set[str] = set()
    for node in nodes:
        _require(isinstance(node, dict), "Nodes must be objects.")
        _keys(node, {"id", "label", "shape"})
        _require(
            _text(node.get("id")) and node["id"] not in identifiers, "Node IDs must be unique."
        )
        _require(_text(node.get("label")), "Every node needs a text label.")
        _require(node.get("shape", "box") in ("box", "ellipse", "diamond"), "Invalid node shape.")
        identifiers.add(node["id"])
    for edge in edges:
        _require(isinstance(edge, dict), "Edges must be objects.")
        _keys(edge, {"from", "to", "label"})
        _require(
            all(
                isinstance(edge.get(key), str) and edge[key] in identifiers
                for key in ("from", "to")
            ),
            "Every edge endpoint must name an existing node.",
        )
        _require(_text(edge.get("label", ""), empty=True), "Invalid edge label.")


def diagram_source(spec: dict[str, Any]) -> str:
    """Quote every label, and use platform-generated DOT identifiers."""

    def quote(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    ids = {node["id"]: f"n{index}" for index, node in enumerate(spec["nodes"])}
    title = spec["title"] + ("\n" + spec["source"] if spec.get("source") else "")
    lines = [
        "digraph visual {",
        f"  graph [rankdir={spec.get('direction', 'LR')}, label={quote(title)}, "
        'labelloc=t, fontname="DejaVu Sans", fontsize=18, bgcolor=white, '
        'size="12.5,7.5", dpi=120, pad=0.25];',
        '  node [fontname="DejaVu Sans", fontsize=12, style=filled, '
        'fillcolor="#E8F3F3", color="#147D82", margin="0.16,0.10"];',
        '  edge [fontname="DejaVu Sans", fontsize=10, color="#52656A"];',
    ]
    for node in spec["nodes"]:
        lines.append(
            f"  {ids[node['id']]} [label={quote(node['label'])}, shape={node.get('shape', 'box')}];"
        )
    for edge in spec["edges"]:
        lines.append(
            f"  {ids[edge['from']]} -> {ids[edge['to']]} [label={quote(edge.get('label', ''))}];"
        )
    return "\n".join([*lines, "}", ""])


def _render_chart(spec: dict[str, Any], directory: Path, formats: list[str]) -> None:
    # Imports stay lazy: worker dependencies and pure validation need no plotting stack.
    with tempfile.TemporaryDirectory(prefix="veetbot-mpl-") as cache:
        os.environ["MPLCONFIGDIR"] = cache
        os.environ["MPLBACKEND"] = "Agg"
        mpl = importlib.import_module("matplotlib")
        mpl.use("Agg", force=True)
        plt = importlib.import_module("matplotlib.pyplot")
        with mpl.rc_context(
            {
                "text.usetex": False,
                "text.parse_math": False,
                "svg.fonttype": "none",
                "font.family": "DejaVu Sans",
                "font.size": 11,
                "axes.spines.top": False,
                "axes.spines.right": False,
            }
        ):
            figure, axes = plt.subplots(figsize=(10, 6), dpi=150)
            try:
                _plot(spec, axes, figure)
                figure.suptitle(spec["title"], fontsize=18, fontweight="bold", x=0.08, ha="left")
                if spec.get("source"):
                    figure.text(0.08, 0.025, spec["source"], fontsize=9, color="#52656A", wrap=True)
                figure.tight_layout(rect=(0.02, 0.08, 0.98, 0.91))
                for extension in formats:
                    figure.savefig(
                        directory / f"chart.{extension}", format=extension, facecolor="white"
                    )
            finally:
                plt.close(figure)
    with (directory / "chart.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(spec["columns"])
        writer.writerows(spec["rows"])


def _plot(spec: dict[str, Any], axes: Any, figure: Any) -> None:
    columns, rows, kind = spec["columns"], spec["rows"], spec["type"]
    xs = [row[columns.index(spec["x"])] for row in rows] if "x" in spec else []
    ys = spec["y"]
    if kind == "heatmap":
        categories = [row[columns.index(ys[0])] for row in rows]
        x_labels, y_labels = list(dict.fromkeys(xs)), list(dict.fromkeys(categories))
        matrix = [[math.nan] * len(x_labels) for _ in y_labels]
        for row, x, y in zip(rows, xs, categories, strict=True):
            value = row[columns.index(spec["value"])]
            matrix[y_labels.index(y)][x_labels.index(x)] = math.nan if value is None else value
        image = axes.imshow(matrix, aspect="auto", interpolation="nearest", cmap="viridis")
        axes.set_facecolor("#E5E5E5")
        axes.set_xticks(range(len(x_labels)), x_labels, rotation=35, ha="right")
        axes.set_yticks(range(len(y_labels)), y_labels)
        figure.colorbar(image, ax=axes, label=spec["value"])
    else:
        for index, y in enumerate(ys):
            values = [row[columns.index(y)] for row in rows]
            numbers = [math.nan if value is None else value for value in values]
            color = COLORS[index]
            if kind == "line":
                axes.plot(
                    xs,
                    numbers,
                    label=y,
                    color=color,
                    linewidth=2,
                    marker="o",
                    markersize=3 if len(rows) <= 100 else 0,
                )
            elif kind == "bar":
                width = 0.8 / len(ys)
                positions = [i - 0.4 + width * (index + 0.5) for i in range(len(xs))]
                axes.bar(positions, numbers, width=width, label=y, color=color)
                axes.set_xticks(range(len(xs)), xs, rotation=35, ha="right")
            elif kind == "scatter":
                axes.scatter(xs, numbers, label=y, color=color, s=24, alpha=0.8)
            elif kind == "histogram":
                axes.hist(
                    [v for v in values if v is not None],
                    bins=spec.get("bins", 20),
                    color=color,
                    edgecolor="white",
                )
            elif kind == "pie":
                axes.pie(
                    numbers,
                    labels=xs,
                    colors=COLORS,
                    autopct="%1.1f%%",
                    wedgeprops={"edgecolor": "white"},
                )
        if len(ys) > 1:
            axes.legend(frameon=False)
        if kind != "pie":
            axes.grid(axis="y", alpha=0.18)
            axes.set_axisbelow(True)
        if kind == "line" and xs and isinstance(xs[0], str):
            categories = list(dict.fromkeys(xs))
            stride = max(1, math.ceil(len(categories) / 16))
            ticks = list(range(0, len(categories), stride))
            axes.set_xticks(ticks, [categories[i] for i in ticks], rotation=35, ha="right")
    if kind != "pie":
        axes.set_xlabel(spec.get("x_label") or (ys[0] if kind == "histogram" else spec["x"]))
        axes.set_ylabel(spec.get("y_label") or ("Count" if kind == "histogram" else ", ".join(ys)))


def _render_diagram(spec: dict[str, Any], directory: Path, formats: list[str]) -> None:
    source = directory / "diagram.dot"
    source.write_text(diagram_source(spec), encoding="utf-8")
    for extension in formats:
        result = subprocess.run(
            [
                "/usr/bin/dot",
                f"-T{extension}",
                str(source),
                "-o",
                str(directory / f"diagram.{extension}"),
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            raise ValueError("Diagram layout failed; simplify the diagram and retry.")


def render(value: object, workspace: Path) -> list[dict[str, Any]]:
    """Publish a complete new workspace directory, or remove all staged output."""
    spec = validate_spec(value)
    formats = ["png", *spec.get("formats", [])]
    directory = Path(tempfile.mkdtemp(prefix=".visual-", dir=workspace))
    try:
        if spec["type"] == "diagram":
            _render_diagram(spec, directory, formats)
        else:
            _render_chart(spec, directory, formats)
        (directory / "source.json").write_text(
            json.dumps(spec, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
            encoding="utf-8",
        )
        paths = sorted(directory.iterdir())
        _require(
            all(0 < path.stat().st_size <= MAX_OUTPUT_BYTES for path in paths),
            "Rendered output exceeds the size limit.",
        )
        # The private staging directory is renamed only after every format succeeds.
        published = directory.with_name(directory.name.removeprefix("."))
        directory.rename(published)
        return [
            {
                "path": f"{published.name}/{path.name}",
                "filename": path.name,
                "media_type": MEDIA_TYPES[path.suffix[1:]],
                "size_bytes": (published / path.name).stat().st_size,
            }
            for path in sorted(paths, key=lambda path: (path.suffix != ".png", path.name))
        ]
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Render static charts/diagrams from bounded JSON.")
    parser.add_argument("spec", help="Workspace JSON file; see /opt/agent/visualization-guide.md")
    args = parser.parse_args()
    try:
        workspace = Path("/workspace").resolve()
        source = (Path.cwd() / args.spec).resolve()
        _require(source.is_relative_to(workspace), "The specification must be in the workspace.")
        with source.open("rb") as stream:
            data = stream.read(MAX_INPUT_BYTES + 1)
        _require(len(data) <= MAX_INPUT_BYTES, "The specification exceeds 1 MiB.")
        value = json.loads(data)
        files = render(value, workspace)
    except (ValueError, OSError, RecursionError, subprocess.TimeoutExpired):
        # Validation errors are actionable; OS/parser details must not dump input bytes.
        error = sys.exception()
        detail = (
            str(error) if type(error) is ValueError else "Cannot read or render the specification."
        )
        print(json.dumps({"ok": False, "error": detail}), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "files": files}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
