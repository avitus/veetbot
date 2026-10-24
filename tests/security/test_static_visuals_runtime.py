"""ADR-0243: the built sandbox provides offline charts and diagrams."""

from __future__ import annotations

import csv
import io
import json
import struct
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID
from xml.etree import ElementTree

import pytest

from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.adapters.determinism import RandomIdFactory, SystemClock
from agent_core.adapters.execution.docker import (
    DockerExecutionEnvironment,
    resolve_local_image_digest,
)
from agent_core.application.artifact_writer import ArtifactWriterFactory
from agent_core.bootstrap import build
from agent_core.domain.artifacts import ArtifactOrigin
from agent_core.domain.errors import NotFoundError
from agent_core.domain.execution import ExecutionCommand, ResourceLimits
from agent_core.execution.manager import SandboxManager
from agent_core.execution.visuals import MEDIA_TYPES
from agent_core.tools.artifact_export import ArtifactExportTool
from tests.contract.support import tool_context
from tests.gates.test_artifact_m6 import _settings
from tests.unit.test_static_visuals import CHART, DIAGRAM

pytestmark = [pytest.mark.integration, pytest.mark.sandbox]
RUN_ID = UUID(int=243)


@pytest.fixture
async def visual_sandbox() -> AsyncIterator[SandboxManager]:
    adapter = DockerExecutionEnvironment(SystemClock(), RandomIdFactory(), hard_cap_seconds=120)
    digest = await resolve_local_image_digest("agent-core-sandbox:dev")
    manager = SandboxManager(
        adapter,
        image_digest=digest,
        limits=ResourceLimits(
            cpu_millicores=2000,
            memory_bytes=2 * 1024**3,
            pids_max=512,
            workspace_bytes=64 * 1024**2,
            inodes_max=10000,
            wall_clock_seconds=120,
        ),
    )
    try:
        yield manager
    finally:
        await manager.close()


async def test_sandbox_offers_offline_plotting_and_graph_layout(
    visual_sandbox: SandboxManager,
) -> None:
    probe = (
        "import importlib.util,json,shutil; "
        "print(json.dumps({'plotting': importlib.util.find_spec('matplotlib') is not None, "
        "'diagrams': shutil.which('dot') is not None}))"
    )
    result = await visual_sandbox.execute_for(
        "tenant-a",
        RUN_ID,
        1,
        ExecutionCommand(("python", "-c", probe), PurePosixPath("."), 20, None, 4096),
    )
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {"plotting": True, "diagrams": True}


async def _render(
    manager: SandboxManager,
    spec: dict[str, Any],
    working_directory: str = ".",
) -> dict[str, bytes]:
    workspace = manager.for_run("tenant-a", RUN_ID, 1)
    await workspace.write(
        str(PurePosixPath(working_directory) / "spec.json"), json.dumps(spec).encode()
    )
    result = await manager.execute_for(
        "tenant-a",
        RUN_ID,
        1,
        ExecutionCommand(
            ("python", "-m", "agent_core.execution.visuals", "spec.json"),
            PurePosixPath(working_directory),
            90,
            None,
            32768,
        ),
    )
    assert result.exit_code == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is True
    assert report["files"][0]["media_type"] == "image/png"
    files = {entry["filename"]: await workspace.read(entry["path"]) for entry in report["files"]}
    for entry in report["files"]:
        assert entry["size_bytes"] == len(files[entry["filename"]])
    return files


@pytest.mark.parametrize("kind", ["line", "bar", "scatter", "histogram", "heatmap", "pie"])
async def test_chart_png_vectors_and_original_csv(
    visual_sandbox: SandboxManager,
    kind: str,
) -> None:
    spec = {
        **CHART,
        "type": kind,
        "formats": ["svg", "pdf"],
        "source": "Owner figures, unchanged",
        "y_label": "USD",
    }
    if kind in {"scatter", "pie"}:
        spec["rows"] = [[1, 12.5], [2, 0], [3, 5]]
    elif kind == "histogram":
        del spec["x"]
    elif kind == "heatmap":
        spec.update(
            columns=["Day", "Hour", "Count"],
            rows=[["Mon", "9", 0], ["Tue", "10", 5]],
            x="Day",
            y=["Hour"],
            value="Count",
        )
    files = await _render(visual_sandbox, spec)
    assert set(files) == {"chart.png", "chart.svg", "chart.pdf", "chart.csv", "source.json"}
    assert files["chart.png"].startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", files["chart.png"][16:24]) == (1500, 900)
    assert files["chart.pdf"].startswith(b"%PDF-")
    texts = " ".join(ElementTree.fromstring(files["chart.svg"]).itertext())
    assert "Revenue ($)" in texts
    assert "Owner figures, unchanged" in texts
    csv_rows = list(csv.reader(io.StringIO(files["chart.csv"].decode())))
    assert csv_rows == [
        spec["columns"],
        *[["" if value is None else str(value) for value in row] for row in spec["rows"]],
    ]
    assert json.loads(files["source.json"]) == spec


async def test_diagram_topology_literal_labels_and_all_downloads(
    visual_sandbox: SandboxManager,
) -> None:
    label = r'Literal \N and <IMG SRC="https://example.com/secret"/>'
    spec = {
        **DIAGRAM,
        "formats": ["svg", "pdf"],
        "nodes": [
            {"id": "start", "label": label},
            {"id": "end", "label": "End", "shape": "diamond"},
        ],
    }
    files = await _render(visual_sandbox, spec)
    assert set(files) == {"diagram.png", "diagram.svg", "diagram.pdf", "diagram.dot", "source.json"}
    assert files["diagram.png"].startswith(b"\x89PNG")
    assert files["diagram.pdf"].startswith(b"%PDF-")
    root = ElementTree.fromstring(files["diagram.svg"])
    ns = {"svg": "http://www.w3.org/2000/svg"}
    assert len(root.findall(".//svg:g[@class='node']", ns)) == 2
    assert len(root.findall(".//svg:g[@class='edge']", ns)) == 1
    assert label in " ".join(root.itertext())
    assert not root.findall(".//svg:image", ns)
    assert not root.findall(".//svg:a", ns)
    assert b"n0 -> n1" in files["diagram.dot"]


async def test_default_output_retry_and_invalid_input(
    visual_sandbox: SandboxManager,
) -> None:
    first = await _render(visual_sandbox, CHART)
    second = await _render(visual_sandbox, CHART)
    assert set(first) == set(second) == {"chart.png", "chart.csv", "source.json"}
    workspace = visual_sandbox.for_run("tenant-a", RUN_ID, 1)
    before = await workspace.listdir("", recursive=True)
    assert sum(str(entry.path).endswith("/chart.png") for entry in before) == 2
    await workspace.write("spec.json", json.dumps({**CHART, "rows": []}).encode())
    result = await visual_sandbox.execute_for(
        "tenant-a",
        RUN_ID,
        1,
        ExecutionCommand(
            ("python", "-m", "agent_core.execution.visuals", "spec.json"),
            PurePosixPath("."),
            30,
            None,
            4096,
        ),
    )
    assert result.exit_code == 2
    assert json.loads(result.stderr)["ok"] is False
    after = await workspace.listdir("", recursive=True)
    assert {str(entry.path) for entry in before} == {str(entry.path) for entry in after}


async def test_plotted_values_are_the_supplied_values(
    visual_sandbox: SandboxManager,
) -> None:
    # Inspect Matplotlib's actual artists, not just a nonempty generated image.
    script = f"""
import json, math, matplotlib
matplotlib.use('Agg')
from matplotlib import pyplot
from agent_core.execution.visuals import _plot, validate_spec
spec = validate_spec({CHART!r})
fig, ax = pyplot.subplots()
_plot(spec, ax, fig)
line = ax.lines[0]
values = [None if math.isnan(v) else v for v in line.get_ydata()]
print(json.dumps({{'x': list(line.get_xdata()), 'y': values}}))
pyplot.close(fig)
"""
    result = await visual_sandbox.execute_for(
        "tenant-a",
        RUN_ID,
        1,
        ExecutionCommand(("python", "-c", script), PurePosixPath("."), 30, None, 4096),
    )
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {"x": ["Jan", "Feb", "Mar"], "y": [12.5, None, 0]}


async def test_repeated_categories_keep_ticks_aligned_with_real_positions(
    visual_sandbox: SandboxManager,
) -> None:
    spec = {**CHART, "rows": [["Jan", 3], ["Feb", 5], ["Jan", 7]]}
    script = f"""
import json, matplotlib
matplotlib.use('Agg')
from matplotlib import pyplot
from agent_core.execution.visuals import _plot, validate_spec
fig, ax = pyplot.subplots()
_plot(validate_spec({spec!r}), ax, fig)
print(json.dumps({{'positions': list(map(float, ax.get_xticks())),
                  'labels': [label.get_text() for label in ax.get_xticklabels()]}}))
pyplot.close(fig)
"""
    result = await visual_sandbox.execute_for(
        "tenant-a",
        RUN_ID,
        1,
        ExecutionCommand(("python", "-c", script), PurePosixPath("."), 30, None, 4096),
    )
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {"positions": [0, 1], "labels": ["Jan", "Feb"]}


@pytest.mark.parametrize("spec", [CHART, DIAGRAM], ids=["chart", "diagram"])
@pytest.mark.parametrize("working_directory", [".", "reports"])
async def test_rendered_downloads_preserve_bytes_and_owner_boundary(
    visual_sandbox: SandboxManager,
    tmp_path: Path,
    spec: dict[str, Any],
    working_directory: str,
) -> None:
    files = await _render(visual_sandbox, {**spec, "formats": ["svg", "pdf"]}, working_directory)
    workspace = visual_sandbox.for_run("tenant-a", RUN_ID, 1)
    entries = await workspace.listdir("", recursive=True)
    async with build(settings=_settings(tmp_path), sequential_ids=True) as composition:
        writer = ArtifactWriterFactory(
            composition.uow_factory,
            FilesystemArtifactStore(composition.settings.artifact_root),
            composition.clock,
            RandomIdFactory(),
        ).for_run(
            tenant_id=composition.principal.tenant_id,
            principal_id=composition.principal.principal_id,
            session_id=UUID(int=244),
            run_id=RUN_ID,
            origin=ArtifactOrigin.SANDBOX_EXPORT,
        )
        context = replace(
            tool_context(),
            principal=composition.principal,
            tenant_id=composition.principal.tenant_id,
            run_id=RUN_ID,
            session_id=UUID(int=244),
            workspace=workspace,
            artifacts=writer,
        )
        for filename, content in files.items():
            path = next(str(entry.path) for entry in entries if entry.path.name == filename)
            media_type = MEDIA_TYPES[filename.rsplit(".", 1)[1]]
            result = await ArtifactExportTool().execute(
                {"path": path, "filename": filename, "media_type": media_type}, context
            )
            assert result.ok and result.structured, result.failure
            artifact_id = UUID(str(result.structured["artifact_id"]))
            opened = await composition.services.artifacts.open_content(
                composition.principal, artifact_id
            )
            downloaded = b"".join([chunk async for chunk in await opened.open()])
            assert downloaded == content
            metadata = await composition.services.artifacts.get(composition.principal, artifact_id)
            assert metadata.media_type == media_type
            # Keep real downloaded files in pytest's temporary directory for visual QA.
            (tmp_path / filename).write_bytes(downloaded)
            with pytest.raises(NotFoundError):
                await composition.services.artifacts.open_content(
                    composition.principal.model_copy(update={"tenant_id": "other-tenant"}),
                    artifact_id,
                )
