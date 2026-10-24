"""Bounded literal data, explicit missing values and atomic visualization output."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from agent_core.execution.visuals import diagram_source, render, validate_spec

CHART: dict[str, Any] = {
    "type": "line",
    "title": "Revenue ($)",
    "columns": ["Month", "Revenue"],
    "rows": [["Jan", 12.5], ["Feb", None], ["Mar", 0]],
    "x": "Month",
    "y": ["Revenue"],
}
DIAGRAM: dict[str, Any] = {
    "type": "diagram",
    "title": "Approval",
    "nodes": [{"id": "start", "label": "Start"}, {"id": "end", "label": "End"}],
    "edges": [{"from": "start", "to": "end", "label": "Approved"}],
}


def test_validation_preserves_zero_missing_values_and_original_order() -> None:
    source = copy.deepcopy(CHART)
    assert validate_spec(source) == CHART
    assert source["rows"] == [["Jan", 12.5], ["Feb", None], ["Mar", 0]]


@pytest.mark.parametrize(
    "replacement",
    [
        {"rows": []},
        {"rows": [["Jan"]]},
        {"rows": [["Jan", float("nan")]]},
        {"rows": [["Jan", float("inf")]]},
        {"rows": [["Jan", True]]},
        {"rows": [["Jan", "12.5"]]},
        {"rows": [["Jan", None]]},
        {"rows": [["Jan", 10**101]]},
        {"columns": ["Month", "Month"]},
        {"y": ["Missing"]},
        {"y": ["Revenue", "Revenue"]},
        {"formats": ["html"]},
        {"formats": ["svg", "svg"]},
        {"formats": [{}]},
        {"url": "https://example.com/data"},
        {"title": "x" * 161},
        {"rows": [["Jan", 2]] * 10001},
        {"type": "scatter"},
        {"bins": 5},
        {"value": "Revenue"},
        {"x": {}},
    ],
)
def test_bad_chart_data_is_rejected_without_creating_files(
    replacement: dict[str, Any],
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError):
        render({**CHART, **replacement}, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_heatmap_refuses_ambiguous_duplicate_cells() -> None:
    spec = {
        "type": "heatmap",
        "title": "Activity",
        "columns": ["Day", "Hour", "Count"],
        "rows": [["Mon", "9", 1], ["Mon", "9", 2]],
        "x": "Day",
        "y": ["Hour"],
        "value": "Count",
    }
    with pytest.raises(ValueError, match="Duplicate heatmap"):
        validate_spec(spec)


@pytest.mark.parametrize(
    "kind,rows",
    [
        ("pie", [["Jan", -1]]),
        ("pie", [["Jan", 0]]),
        ("pie", [["Jan", None]]),
        ("bar", [["Jan", 1], ["Jan", 2]]),
    ],
)
def test_pie_and_bar_require_unambiguous_values(kind: str, rows: list[list[Any]]) -> None:
    with pytest.raises(ValueError):
        validate_spec({**CHART, "type": kind, "rows": rows})


@pytest.mark.parametrize(
    "replacement",
    [
        {"nodes": []},
        {"nodes": [{"id": "x", "label": "One"}, {"id": "x", "label": "Two"}]},
        {"edges": [{"from": "start", "to": "unknown"}]},
        {"nodes": [{"id": "x", "label": "One", "image": "/etc/passwd"}]},
        {"nodes": [{"id": "x", "label": "One", "shape": "record"}]},
        {"edges": [{"from": "start", "to": "end", "URL": "https://example.com"}]},
        {"direction": "RL"},
        {"edges": [{}] * 121},
        {"nodes": [None]},
    ],
)
def test_diagram_validation_refuses_ambiguous_or_active_content(
    replacement: dict[str, Any],
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError):
        render({**DIAGRAM, **replacement}, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_diagram_labels_cannot_inject_dot_attributes_or_node_ids() -> None:
    spec = {
        **DIAGRAM,
        "nodes": [
            {"id": '"; injected -> n0', "label": '<IMG SRC="/etc/passwd"/>'},
            {"id": "end", "label": r'Literal \N and "quotes"'},
        ],
        "edges": [{"from": '"; injected -> n0', "to": "end"}],
    }
    source = diagram_source(validate_spec(spec))
    assert "injected" not in source
    assert 'n0 -> n1 [label=""]' in source
    assert r'label="<IMG SRC=\"/etc/passwd\"/>"' in source
    assert r'label="Literal \\N and \"quotes\""' in source


def test_failed_renderer_removes_every_staged_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_spec: dict[str, Any], directory: Path, _formats: list[str]) -> None:
        (directory / "chart.png").write_bytes(b"partial")
        raise ValueError("requested PDF failed")

    monkeypatch.setattr("agent_core.execution.visuals._render_chart", fail)
    with pytest.raises(ValueError, match="requested PDF failed"):
        render(CHART, tmp_path)
    assert list(tmp_path.iterdir()) == []
