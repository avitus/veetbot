"""Milestone 6 artifact checksum, API, and tenant-boundary gate."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

import httpx
import pytest

from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.adapters.execution.local_workspace import LocalWorkspaceHandle
from agent_core.api import create_app
from agent_core.application.artifact_writer import ArtifactWriterFactory
from agent_core.bootstrap import build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.agents import Principal
from agent_core.domain.artifacts import ArtifactMetadata, ArtifactOrigin
from agent_core.domain.errors import ArtifactIntegrityError, NotFoundError
from agent_core.domain.messages import FileReferencePart, TextPart
from agent_core.domain.policies import (
    IdempotencyClass,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.runs import Run, RunLimits, RunStatus
from agent_core.domain.tools import ToolExecutionContext, ToolResult, ToolSpec
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.tools.artifact_export import ArtifactExportTool
from agent_core.tools.executor import ToolPipeline
from agent_core.tools.registry import StaticToolRegistry
from tests.contract.support import tool_context


async def _chunks(content: bytes) -> AsyncIterator[bytes]:
    for offset in range(0, len(content), 4093):
        yield content[offset : offset + 4093]


class _UnavailableCollaborator:
    def __getattr__(self, name: str) -> object:
        raise RuntimeError(f"collaborator {name!r} unavailable")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://unused/agent",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials=MappingProxyType({}),
        interpolation=MappingProxyType({"OPENAI_MODEL": ""}),
        artifact_root=tmp_path / "artifacts",
    )


async def test_artifact_checksum(tmp_path: Path) -> None:
    content = b"streamed-sandbox-output\n" * 10_000
    run_id = UUID(int=9001)
    session_id = UUID(int=9000)
    async with build(settings=_settings(tmp_path), sequential_ids=True) as composition:
        store = FilesystemArtifactStore(composition.settings.artifact_root)
        workspace = composition.sandbox.for_run(
            composition.principal.tenant_id, run_id, lease_epoch=1
        )
        await workspace.write("generated/result.bin", content)
        writer = ArtifactWriterFactory(
            composition.uow_factory,
            store,
            composition.clock,
            SequenceIdFactory([UUID(int=10_000)]),
        ).for_run(
            tenant_id=composition.principal.tenant_id,
            principal_id=composition.principal.principal_id,
            session_id=session_id,
            run_id=run_id,
            origin=ArtifactOrigin.SANDBOX_EXPORT,
        )
        result = await ArtifactExportTool().execute(
            {
                "path": "generated/result.bin",
                "filename": "../../result.bin",
                "media_type": "application/octet-stream",
            },
            replace(
                tool_context(),
                run_id=run_id,
                session_id=session_id,
                tenant_id=composition.principal.tenant_id,
                principal=composition.principal,
                lease_epoch=1,
                workspace=workspace,
                artifacts=writer,
            ),
        )
        assert result.ok is True
        assert result.structured is not None
        artifact_id = UUID(str(result.structured["artifact_id"]))
        ref = await composition.services.artifacts.get(composition.principal, artifact_id)
        app = create_app(
            composition.services,
            composition.settings,
            composition.principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 43101)),
            base_url="http://agent.test",
        ) as client:
            metadata = await client.get(f"/v1/artifacts/{ref.id}")
            downloaded = await client.get(f"/v1/artifacts/{ref.id}/content")
        assert metadata.status_code == 200
        assert downloaded.status_code == 200
        assert downloaded.content == content
        assert hashlib.sha256(downloaded.content).hexdigest() == ref.sha256
        other_tenant = Principal(
            tenant_id="tenant-b",
            principal_id=composition.principal.principal_id,
            scopes=set(PLATFORM_SCOPES),
        )
        with pytest.raises(NotFoundError):
            await composition.services.artifacts.get(other_tenant, ref.id)

        now = composition.clock.now()
        mismatch = ArtifactMetadata(
            artifact_id=UUID(int=9999),
            tenant_id=composition.principal.tenant_id,
            principal_id=composition.principal.principal_id,
            session_id=UUID(int=9000),
            run_id=UUID(int=9001),
            origin=ArtifactOrigin.SANDBOX_EXPORT,
            filename="ignored.bin",
            media_type="application/octet-stream",
            size_bytes=len(content),
            sha256="0" * 64,
            trust=TrustLevel.EXTERNAL_UNTRUSTED,
            created_at=now,
            expires_at=now + timedelta(days=30),
        )
        with pytest.raises(ArtifactIntegrityError):
            await store.put(_chunks(content), mismatch)
        assert not list(composition.settings.artifact_root.rglob(str(mismatch.artifact_id)))


async def test_artifact_export_normalizes_an_unavailable_workspace() -> None:
    result = await ArtifactExportTool().execute(
        {
            "path": "result.bin",
            "filename": "result.bin",
            "media_type": "application/octet-stream",
        },
        replace(
            tool_context(),
            workspace=_UnavailableCollaborator(),
            artifacts=object(),
        ),
    )
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.reason_code == "tool.internal_error"


async def test_artifact_export_normalizes_an_unavailable_writer(tmp_path: Path) -> None:
    workspace = LocalWorkspaceHandle(tmp_path / "workspace")
    await workspace.write("result.bin", b"result")
    result = await ArtifactExportTool().execute(
        {
            "path": "result.bin",
            "filename": "result.bin",
            "media_type": "application/octet-stream",
        },
        replace(
            tool_context(),
            workspace=workspace,
            artifacts=_UnavailableCollaborator(),
        ),
    )
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.reason_code == "tool.internal_error"


async def test_generated_workspace_file_exports_as_authorized_artifact(tmp_path: Path) -> None:
    content = b"generated in the sandbox\n"
    run_id = UUID(int=15_001)
    session_id = UUID(int=15_002)
    async with build(settings=_settings(tmp_path), sequential_ids=True) as composition:
        workspace = composition.sandbox.for_run(
            composition.principal.tenant_id, run_id, lease_epoch=1
        )
        await workspace.write("output/report.txt", content)
        writer = ArtifactWriterFactory(
            composition.uow_factory,
            FilesystemArtifactStore(composition.settings.artifact_root),
            composition.clock,
            SequenceIdFactory([UUID(int=15_003)]),
        ).for_run(
            tenant_id=composition.principal.tenant_id,
            principal_id=composition.principal.principal_id,
            session_id=session_id,
            run_id=run_id,
            origin=ArtifactOrigin.SANDBOX_EXPORT,
        )
        context = replace(
            tool_context(),
            run_id=run_id,
            session_id=session_id,
            tenant_id=composition.principal.tenant_id,
            principal=composition.principal,
            lease_epoch=1,
            workspace=workspace,
            artifacts=writer,
        )
        result = await ArtifactExportTool().execute(
            {
                "path": "output/report.txt",
                "filename": "report.txt",
                "media_type": "text/plain",
            },
            context,
        )
        assert result.ok is True
        assert result.structured is not None
        artifact_id = UUID(str(result.structured["artifact_id"]))
        fetched = await composition.services.artifacts.open_content(
            composition.principal, artifact_id
        )
        assert b"".join([chunk async for chunk in fetched.open()]) == content


class _LargeOutputTool:
    spec = ToolSpec(
        name="demo.large_output",
        version="1.0.0",
        description="test fixture",
        input_schema={"type": "object"},
        output_schema=None,
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        timeout_seconds=5,
        maximum_output_bytes=1000,
        allow_parallel=False,
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )

    async def execute(
        self, arguments: dict[str, object], context: ToolExecutionContext
    ) -> ToolResult:
        del arguments, context
        return ToolResult(ok=True, content=[TextPart(text="x" * 5000)])


async def test_large_tool_output_is_excerpted_and_artifactized(tmp_path: Path) -> None:
    async with build(settings=_settings(tmp_path), sequential_ids=True) as composition:
        store = FilesystemArtifactStore(composition.settings.artifact_root)
        writers = ArtifactWriterFactory(
            composition.uow_factory,
            store,
            composition.clock,
            SequenceIdFactory([UUID(int=20_000)]),
        )
        registry = StaticToolRegistry()
        tool = _LargeOutputTool()
        registry.register(tool)
        pipeline = ToolPipeline(
            registry,
            composition.uow_factory,
            composition.clock,
            SequenceIdFactory(),
            artifact_writers=writers,
        )
        now = composition.clock.now()
        run = Run(
            id=UUID(int=20_001),
            session_id=UUID(int=20_002),
            tenant_id=composition.principal.tenant_id,
            agent_id=UUID(int=20_003),
            agent_version="1.0.0",
            status=RunStatus.RUNNING,
            limits=RunLimits(max_steps=1, max_model_calls=1, max_tool_calls=1),
            created_at=now,
            updated_at=now,
        )
        original = await tool.execute({}, object())  # type: ignore[arg-type]
        result = await pipeline._artifactize_large_output(
            result=original,
            tool=tool,
            run=run,
            principal=composition.principal,
        )
        assert result.metrics["output_bytes"] == 5027
        assert result.metrics["captured_bytes"] == 4000
        assert result.metrics["discarded_bytes"] == 1027
        assert result.metrics["truncated"] == 1
        reference = next(part for part in result.content if isinstance(part, FileReferencePart))
        assert "bytes elided" in result.content[0].text  # type: ignore[union-attr]
        fetched = await composition.services.artifacts.open_content(
            composition.principal, reference.artifact_id
        )
        stored = b"".join([chunk async for chunk in fetched.open()])
        assert len(stored) == result.metrics["captured_bytes"]
        assert "captured first" in result.content[0].text  # type: ignore[union-attr]
