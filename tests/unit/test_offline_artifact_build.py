"""One approved invocation survives the workspace loss at an approval pause."""

from dataclasses import replace
from importlib.resources import files
from typing import Any, cast

import pytest

from agent_core.adapters.execution.fake import FakeExecutionEnvironment, fake_image_digest
from agent_core.bootstrap import build
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.artifacts import StoredArtifactRef
from agent_core.domain.execution import (
    EgressMode,
    EnvironmentHandle,
    ExecutionCommand,
    ResourceLimits,
)
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn
from agent_core.domain.policies import SideEffectClass, TrustLevel
from agent_core.domain.runs import RunStatus
from agent_core.execution.manager import SandboxManager
from agent_core.tools.artifact_build import ArtifactBuildTool
from agent_core.tools.artifact_read_guide import ArtifactReadGuideTool
from agent_core.tools.registry import StaticToolRegistry
from tests.contract.support import tool_context
from tests.integration.m2_support import memory_settings
from tests.unit.test_artifact_export_content import _RecordingWriter
from tests.unit.test_web_tools import FakeWebProvider


async def test_offline_build_and_bundled_guide_are_available() -> None:
    async with build(settings=memory_settings()) as composition:
        registry = cast(StaticToolRegistry, composition.tool_pipeline._registry)
        names = {name for name, _version in registry._tools}
        assert {"artifact.build", "artifact.read_guide"} <= names
        assert registry.get("artifact.build").spec.side_effect is SideEffectClass.CODE_EXECUTION
        assert registry.get("artifact.read_guide").spec.side_effect is SideEffectClass.NONE


def package() -> dict[str, Any]:
    return {
        "files": {"render.py": "print('render')", "data.json": "[2,3]"},
        "command": ["python", "render.py"],
        "outputs": [
            {"path": "result.png", "filename": "infographic.png", "media_type": "image/png"}
        ],
    }


def turns() -> list[ScriptedTurn]:
    return [
        ScriptedTurn(
            tool_calls=[
                ScriptedToolCall(name="web.search", arguments={"query": "Infographic source data"})
            ]
        ),
        ScriptedTurn(
            tool_calls=[
                ScriptedToolCall(name="artifact.read_guide", arguments={"section": "build"})
            ]
        ),
        ScriptedTurn(tool_calls=[ScriptedToolCall(name="artifact.build", arguments=package())]),
        ScriptedTurn(text="Build result reported."),
    ]


async def test_one_approval_survives_empty_workspace_and_attaches_before_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = FakeModelScript(turns=turns())
    async with build(
        settings=memory_settings(),
        script=script,
        enabled_tools=["web.search", "artifact.read_guide", "artifact.build"],
        web_search_provider_override=FakeWebProvider(),
    ) as composition:
        adapter = cast(FakeExecutionEnvironment, composition.sandbox.adapter)
        execute = adapter.execute
        staged = []

        async def render(handle: EnvironmentHandle, command: ExecutionCommand) -> Any:
            if command.argv == ("python", "render.py"):
                workspace = adapter.workspace(handle)
                staged.append(
                    (await workspace.read("render.py"), await workspace.read("data.json"))
                )
                await workspace.write("result.png", b"rendered-image")
            return await execute(handle, command)

        monkeypatch.setattr(adapter, "execute", render)
        run_id = await composition.runs.submit("Create the infographic.")
        assert (await composition.runs.get(run_id)).status is RunStatus.WAITING_FOR_APPROVAL
        [approval] = await composition.approvals.list_pending(run_id=run_id)
        assert approval.tool_name == "artifact.build"
        assert "offline file" in approval.action_summary
        assert approval.arguments["files"] == package()["files"]
        assert staged == []
        assert len(adapter.commands) == 0
        assert adapter.specifications == []
        await composition.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        assert (await composition.runs.get(run_id)).status is RunStatus.COMPLETED
        assert await composition.approvals.list_pending(run_id=run_id) == []
        assert staged == [(b"print('render')", b"[2,3]")]
        assert len(adapter.commands) == 1
        assert not adapter.live_environment_ids()
        spec = adapter.specifications[-1]
        assert spec.egress.mode is EgressMode.DENY
        assert spec.bridge is None
        assert not any("KEY" in key or "TOKEN" in key for key in spec.environment)
        async with composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)
            built = next(item for item in invocations if item.tool_name == "artifact.build")
            assert built.origin_trust is TrustLevel.EXTERNAL_UNTRUSTED
            assert built.structured_result is not None
            assert built.structured_result["complete"] is True
            ref = built.structured_result["attached"][0]
        from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore

        store = FilesystemArtifactStore(composition.settings.artifact_root)
        stream = await store.open_verified(
            StoredArtifactRef(**ref), tenant_id=composition.principal.tenant_id
        )
        assert b"".join([chunk async for chunk in stream]) == b"rendered-image"
        await composition.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        assert len(adapter.commands) == 1
        changed = package()
        changed["files"]["data.json"] = "[3,4]"
        script.turns.extend(
            [
                ScriptedTurn(
                    tool_calls=[ScriptedToolCall(name="artifact.build", arguments=changed)]
                ),
                ScriptedTurn(text="Second build reported."),
            ]
        )
        second = await composition.runs.submit("Use the updated data.", session_id=built.session_id)
        [second_approval] = await composition.approvals.list_pending(run_id=second)
        assert second_approval.id != approval.id
        assert len(adapter.commands) == 1


async def test_denied_build_never_stages_or_executes() -> None:
    async with build(
        settings=memory_settings(),
        script=FakeModelScript(turns=turns()),
        enabled_tools=["web.search", "artifact.read_guide", "artifact.build"],
        web_search_provider_override=FakeWebProvider(),
    ) as composition:
        adapter = cast(FakeExecutionEnvironment, composition.sandbox.adapter)
        run_id = await composition.runs.submit("Create the infographic.")
        [approval] = await composition.approvals.list_pending(run_id=run_id)
        await composition.approvals.resolve(approval.id, ApprovalResolutionType.DENY)
        assert len(adapter.commands) == 0
        assert len(adapter.specifications) == 0


@pytest.mark.parametrize("missing_scope", ["sandbox.execute", "workspace.write", "artifact.write"])
async def test_missing_build_scope_never_executes(missing_scope: str) -> None:
    from agent_core.policy.scopes import PLATFORM_SCOPES
    from tests.contract.support import principal

    owner = principal().model_copy(update={"scopes": set(PLATFORM_SCOPES) - {missing_scope}})
    async with build(
        settings=memory_settings(),
        principal=owner,
        script=FakeModelScript(turns=turns()),
        enabled_tools=["web.search", "artifact.read_guide", "artifact.build"],
        web_search_provider_override=FakeWebProvider(),
    ) as composition:
        run_id = await composition.runs.submit("Create the infographic.")
        assert (await composition.runs.get(run_id)).status is RunStatus.COMPLETED
        assert await composition.approvals.list_pending(run_id=run_id) == []
        adapter = cast(FakeExecutionEnvironment, composition.sandbox.adapter)
        assert not adapter.specifications


async def test_cancellation_destroys_the_build_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
    from tests.contract.support import NOW

    adapter = FakeExecutionEnvironment(FixedClock(NOW), SequenceIdFactory())
    started = asyncio.Event()

    async def wait(*_: object) -> Any:
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(adapter, "execute", wait)
    tool = ArtifactBuildTool(
        lambda: SandboxManager(
            adapter,
            image_digest=fake_image_digest(),
            limits=ResourceLimits(1000, 64 * 1024**2, 32, 64 * 1024**2, 100, 30),
        )
    )
    task = asyncio.create_task(
        tool.execute(package(), replace(tool_context(), artifacts=_RecordingWriter()))
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not adapter.live_environment_ids()


@pytest.mark.parametrize("section", ["build", "charts", "diagrams"])
async def test_guide_is_bounded_packaged_text_without_execution(section: str) -> None:
    result = await ArtifactReadGuideTool().execute({"section": section}, tool_context())
    assert result.ok
    assert result.structured is not None
    text = result.structured["text"]
    assert len(text.encode()) < 4096  # No truncation loop to read this section.
    assert text in files("agent_core.execution").joinpath("visualization-guide.md").read_text()


@pytest.mark.parametrize(
    "mutation",
    [
        {"files": {"../escape.py": "pass"}},
        {"files": {"/etc/evil": "pass"}},
        {"files": {"a": "é" * 524289}},
        {"outputs": [{"path": "../file", "filename": "file", "media_type": "text/plain"}]},
        {"outputs": [{"path": "result.png", "filename": "..", "media_type": "image/png"}]},
        {"command": ["python\u0000"]},
        {"command": ["./render.py"]},
        {"command": "python render.py"},
        {"network": True},
    ],
)
async def test_invalid_build_never_provisions(mutation: dict[str, Any]) -> None:
    from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
    from tests.contract.support import NOW

    adapter = FakeExecutionEnvironment(FixedClock(NOW), SequenceIdFactory())
    tool = ArtifactBuildTool(
        lambda: SandboxManager(
            adapter,
            image_digest=fake_image_digest(),
            limits=ResourceLimits(1000, 64 * 1024**2, 32, 64 * 1024**2, 100, 30),
        )
    )
    result = await tool.execute(package() | mutation, tool_context())
    assert not result.ok
    assert adapter.specifications == []


@pytest.mark.parametrize("exit_code,missing,attached_count", [(1, False, 0), (0, True, 1)])
async def test_failed_execution_or_export_reports_exact_attachments_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    missing: bool,
    attached_count: int,
) -> None:
    from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
    from tests.contract.support import NOW

    adapter = FakeExecutionEnvironment(FixedClock(NOW), SequenceIdFactory())
    execute = adapter.execute

    async def render(handle: EnvironmentHandle, command: ExecutionCommand) -> Any:
        await adapter.workspace(handle).write("result.png", b"image")
        return replace(await execute(handle, command), exit_code=exit_code)

    monkeypatch.setattr(adapter, "execute", render)
    tool = ArtifactBuildTool(
        lambda: SandboxManager(
            adapter,
            image_digest=fake_image_digest(),
            limits=ResourceLimits(1000, 64 * 1024**2, 32, 64 * 1024**2, 100, 30),
        )
    )
    writer = _RecordingWriter()
    arguments = package()
    if missing:
        arguments["outputs"].append(
            {"path": "missing.txt", "filename": "missing.txt", "media_type": "text/plain"}
        )
    result = await tool.execute(arguments, replace(tool_context(), artifacts=writer))
    assert not result.ok
    assert len(result.artifacts) == len(writer.calls) == attached_count
    assert not adapter.live_environment_ids()
