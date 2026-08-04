from pathlib import Path, PurePosixPath

import pytest

from agent_core.adapters.execution.local_workspace import LocalWorkspaceHandle
from agent_core.domain.errors import WorkspaceEscape
from agent_core.domain.execution import WorkspaceProvenance


async def test_workspace_handle_contains_paths_and_records_provenance(tmp_path: Path) -> None:
    handle = LocalWorkspaceHandle(tmp_path / "workspace")
    assert handle.root == PurePosixPath("/workspace")
    assert handle.resolve("notes/a.txt") == PurePosixPath("/workspace/notes/a.txt")
    await handle.write("notes/a.txt", b"hello")
    assert await handle.read("notes/a.txt") == b"hello"
    assert await handle.provenance("notes/a.txt") is WorkspaceProvenance.TOOL_WRITTEN
    assert [str(entry.path) for entry in await handle.listdir("", recursive=True)] == [
        "notes",
        "notes/a.txt",
    ]
    for invalid in ("../outside", "/etc/passwd", "a//b", "a/./b", "nul\x00path"):
        with pytest.raises(WorkspaceEscape):
            handle.resolve(invalid)
