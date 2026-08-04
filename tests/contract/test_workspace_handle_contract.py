import os
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


async def test_workspace_stream_refuses_symlinks_and_special_files(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    handle = LocalWorkspaceHandle(root)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    (root / "link.txt").symlink_to(outside)
    with pytest.raises(WorkspaceEscape):
        await anext(handle.stream("link.txt", 1024))

    fifo = root / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(IsADirectoryError):
        await anext(handle.stream("pipe", 1024))
