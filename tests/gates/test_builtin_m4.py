from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

from agent_core.adapters.execution.local_workspace import LocalWorkspaceHandle
from agent_core.bootstrap import build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.policies import TrustLevel
from agent_core.domain.tools import ToolInvocationStatus
from agent_core.tools.workspace.list_files import WorkspaceListFilesTool
from agent_core.tools.workspace.read_text import WorkspaceReadTextTool
from agent_core.tools.workspace.write_text import WorkspaceWriteTextTool
from tests.contract.support import tool_context

ROOT = Path(__file__).resolve().parents[2]


def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/unused",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials={},
        interpolation={"OPENAI_MODEL": ""},
        artifact_root=tmp_path / "artifacts",
    )


def test_handle_only() -> None:
    forbidden_imports = {"os", "pathlib", "shutil", "glob"}
    for name in ("read_text.py", "write_text.py", "list_files.py"):
        tree = ast.parse(
            (ROOT / "src/agent_core/tools/workspace" / name).read_text(encoding="utf-8")
        )
        imports = {
            alias.name.partition(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        assert imports.isdisjoint(forbidden_imports)
        assert not [
            node for node in calls if isinstance(node.func, ast.Name) and node.func.id == "open"
        ]
        assert not [
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "context"
            and node.attr != "workspace"
        ]


async def test_text_only(tmp_path: Path) -> None:
    handle = LocalWorkspaceHandle(tmp_path / "workspace")
    context = replace(tool_context(), workspace=handle)
    await handle.write("binary.txt", b"a\x00b")
    binary = await WorkspaceReadTextTool().execute({"path": "binary.txt"}, context)
    assert not binary.ok
    assert binary.failure is not None
    assert binary.failure.reason_code == "tool.invalid_arguments.not_text"
    split = b"a" * 4095 + "€".encode()
    await handle.write("split.txt", split)
    text = await WorkspaceReadTextTool().execute({"path": "split.txt"}, context)
    assert text.ok
    assert text.structured is not None
    assert text.structured["content"] == "a" * 4095 + "€"


async def test_write_idempotent(tmp_path: Path) -> None:
    handle = LocalWorkspaceHandle(tmp_path / "workspace")
    context = replace(tool_context(), workspace=handle)
    tool = WorkspaceWriteTextTool()
    first = await tool.execute({"path": "a.md", "content": "hello"}, context)
    second = await tool.execute({"path": "a.md", "content": "hello"}, context)
    assert first.structured is not None and second.structured is not None
    assert first.structured["created"] is True
    assert second.structured["created"] is False
    assert first.structured["checksum"] == second.structured["checksum"]
    assert first.structured["byte_count"] == second.structured["byte_count"] == 5
    assert await handle.read("a.md") == b"hello"


async def test_listing_stable(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    for index in range(1002):
        (root / f"file-{1001 - index:04d}.txt").write_text(str(index), encoding="utf-8")
    (root / "outside-link").symlink_to(tmp_path / "outside")
    handle = LocalWorkspaceHandle(root)
    result = await WorkspaceListFilesTool().execute(
        {"path": "", "recursive": False}, replace(tool_context(), workspace=handle)
    )
    assert result.ok and result.structured is not None
    paths = [entry["path"] for entry in result.structured["entries"]]
    assert paths == sorted(paths, key=lambda value: value.encode("utf-8"))
    assert len(paths) == 1000
    assert result.structured["truncated"] is True
    assert "outside-link" not in paths


async def test_provenance(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    handle = LocalWorkspaceHandle(root)
    context = replace(tool_context(), workspace=handle)
    await WorkspaceWriteTextTool().execute({"path": "a.md", "content": "agent"}, context)
    (root / "b.md").write_text("fixture", encoding="utf-8")
    written = await WorkspaceReadTextTool().execute({"path": "a.md"}, context)
    fixture = await WorkspaceReadTextTool().execute({"path": "b.md"}, context)
    listing = await WorkspaceListFilesTool().execute({"path": ""}, context)
    assert written.output_trust is TrustLevel.INTERNAL_TOOL
    assert fixture.output_trust is TrustLevel.EXTERNAL_UNTRUSTED
    assert listing.output_trust is TrustLevel.EXTERNAL_UNTRUSTED


async def test_demo_records(tmp_path: Path) -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="demo.external_write",
                        arguments={"destination": "demo", "content": "hello"},
                        call_id="demo-write",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="done", stop_reason=StopReason.END_TURN),
        ]
    )
    async with build(settings=settings(tmp_path), script=script, sequential_ids=True) as app:
        run_id = await app.runs.submit("record a demo write")
        approval = (await app.approvals.list_pending())[0]
        await app.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        async with app.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(
                run_id,
                Principal(tenant_id="local", principal_id="local-user"),
            )
        assert len(invocations) == 1
        assert invocations[0].status is ToolInvocationStatus.SUCCEEDED
        assert invocations[0].structured_result == {
            "recorded": True,
            "destination": "demo",
            "byte_count": 5,
            "checksum": ("sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"),
        }
