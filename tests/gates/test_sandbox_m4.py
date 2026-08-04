from __future__ import annotations

from pathlib import Path, PurePosixPath

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from agent_core.adapters.execution.local_workspace import LocalWorkspaceHandle
from agent_core.domain.errors import WorkspaceEscape


@given(st.text(min_size=0, max_size=300))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_workspace_containment(tmp_path: Path, candidate: str) -> None:
    handle = LocalWorkspaceHandle(tmp_path / "workspace")
    try:
        resolved = handle.resolve(candidate)
    except WorkspaceEscape:
        return
    assert isinstance(resolved, PurePosixPath)
    assert resolved == handle.root or handle.root in resolved.parents


def test_workspace_containment_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "escape").symlink_to(tmp_path)
    handle = LocalWorkspaceHandle(root)
    try:
        handle.resolve("escape/outside.txt")
    except WorkspaceEscape:
        pass
    else:
        raise AssertionError("outward symlink was accepted")
