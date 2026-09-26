"""ADR-0129: only the public resolution service may approve for a task.

The internal approval service (evaluations, the archive command) and the
public service without task grants composed both refuse ``approve_for_task``
and leave the approval pending.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from agent_core.bootstrap import build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.approvals import ApprovalResolutionType, ApprovalStatus
from agent_core.domain.browser_task_grants import TaskGrantEcho
from agent_core.domain.errors import ConflictError
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.views import TextContentBlock
from agent_core.policy.scopes import PLATFORM_SCOPES


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
        auth_scopes=PLATFORM_SCOPES,
    )


def _write_script() -> FakeModelScript:
    return FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="demo.external_write",
                        arguments={"destination": "demo", "content": "lesson"},
                        call_id="approve-for-task",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            )
        ]
    )


async def _pending_approval(composition: Any) -> Any:
    session = await composition.services.sessions.create(composition.principal, "general", {})
    submitted = await composition.services.runs.submit(
        composition.principal,
        session.id,
        [TextContentBlock(text="write it")],
        None,
        None,
    )
    return (await composition.approvals.list_pending(run_id=submitted.run_id))[0]


async def test_internal_service_refuses_approve_for_task(tmp_path: Path) -> None:
    async with build(
        settings=_settings(tmp_path), sequential_ids=True, script=_write_script()
    ) as composition:
        approval = await _pending_approval(composition)

        with pytest.raises(ValueError, match="approve_for_task"):
            await composition.approvals.resolve(
                approval.id, ApprovalResolutionType.APPROVE_FOR_TASK
            )
        stored = await composition.approvals.get(approval.id)

    assert stored.status is ApprovalStatus.PENDING


async def test_public_resolve_refuses_approve_for_task_until_task_grants_exist(
    tmp_path: Path,
) -> None:
    async with build(
        settings=_settings(tmp_path), sequential_ids=True, script=_write_script()
    ) as composition:
        approval = await _pending_approval(composition)

        with pytest.raises(ConflictError) as refused:
            await composition.services.approvals.resolve(
                composition.principal,
                approval.id,
                ApprovalResolutionType.APPROVE_FOR_TASK,
                None,
                task_grant=TaskGrantEcho(origin="https://www.example.org", path_prefix="/lesson"),
            )
        stored = await composition.approvals.get(approval.id)

    assert refused.value.reason == "task_grant_unavailable"
    assert stored.status is ApprovalStatus.PENDING
