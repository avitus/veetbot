"""The maintenance pass runs the approval expiry reaper.

Regression coverage for the Milestone 4 spec-versus-implementation gap in
which `ApprovalService.expire_due` implemented the reaper the plan requires
("A periodic reaper must expire approvals past expires_at") but nothing in
the composition ever called it, so an approval past `expires_at` stayed
pending and its run parked forever.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import cast

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalStatus
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.runs import RunStatus
from agent_core.domain.tools import ToolInvocationStatus
from agent_core.runtime.worker import MaintenanceWorker

_START = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/unused",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials=MappingProxyType({}),
        interpolation=MappingProxyType({"OPENAI_MODEL": ""}),
        artifact_root=tmp_path / "artifacts",
    )


def _script() -> FakeModelScript:
    return FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="demo.external_write",
                        arguments={"destination": "demo", "content": "hello"},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="expiry acknowledged", stop_reason=StopReason.END_TURN),
        ]
    )


async def test_maintenance_pass_expires_due_approvals_and_resumes_the_run(
    tmp_path: Path,
) -> None:
    clock = FixedClock(_START)
    async with build(settings=_settings(tmp_path), script=_script(), clock=clock) as app:
        run_id = await app.runs.submit("request an external write")
        assert (await app.runs.get(run_id)).status is RunStatus.WAITING_FOR_APPROVAL
        approval = (await app.approvals.list_pending(run_id=run_id))[0]
        assert approval.expires_at is not None
        clock.advance(approval.expires_at - clock.now() + timedelta(seconds=1))

        maintenance = cast(MaintenanceWorker, app.maintenance_factory())
        await maintenance.run_once()

        assert (await app.approvals.get(approval.id)).status is ApprovalStatus.EXPIRED
        assert (await app.runs.get(run_id)).status is RunStatus.COMPLETED
        actor = Principal(tenant_id="local", principal_id="local-user")
        async with app.uow_factory() as uow:
            invocation = (await uow.invocations.list_for_run(run_id, actor))[0]
            events = await uow.events.list_after(approval.session_id, 0, app.principal)
        assert invocation.status is ToolInvocationStatus.DENIED
        assert invocation.outcome is not None
        assert invocation.outcome.reason_code == "approval.expired"
        expiries = [
            event
            for event in events
            if event.event_type == "approval.resolved"
            and event.payload.get("resolution") == "expired"
        ]
        assert [event.payload.get("approval_id") for event in expiries] == [str(approval.id)]


async def test_undue_approvals_survive_the_maintenance_pass(tmp_path: Path) -> None:
    clock = FixedClock(_START)
    async with build(settings=_settings(tmp_path), script=_script(), clock=clock) as app:
        run_id = await app.runs.submit("request an external write")
        approval = (await app.approvals.list_pending(run_id=run_id))[0]

        maintenance = cast(MaintenanceWorker, app.maintenance_factory())
        await maintenance.run_once()

        assert (await app.approvals.get(approval.id)).status is ApprovalStatus.PENDING
        assert (await app.runs.get(run_id)).status is RunStatus.WAITING_FOR_APPROVAL


async def test_approval_sweep_failure_does_not_abort_the_maintenance_pass(
    tmp_path: Path,
) -> None:
    clock = FixedClock(_START)
    approval_sweeps = 0
    export_sweeps = 0

    async def failing_sweep() -> int:
        nonlocal approval_sweeps
        approval_sweeps += 1
        raise RuntimeError("approval sweep exploded")

    async def counting_sweep() -> int:
        nonlocal export_sweeps
        export_sweeps += 1
        return 0

    async with build(settings=_settings(tmp_path), script=_script(), clock=clock) as app:
        worker = MaintenanceWorker(
            uow_factory=app.uow_factory,
            clock=clock,
            sweep_approvals=failing_sweep,
            sweep_exports=counting_sweep,
        )
        await worker.run_once()

    assert approval_sweeps == 1
    assert export_sweeps == 1
