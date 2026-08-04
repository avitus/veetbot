"""Durable Milestone 4 approval parking and restart verification."""

from __future__ import annotations

from agent_core.bootstrap import build
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.runs import RunStatus
from agent_core.domain.tools import ToolInvocationStatus
from agent_core.runtime.worker import DurableWorker
from tests.integration.m2_support import database_settings


async def test_approval_resumes_after_worker_and_composition_restart() -> None:
    proposed = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="demo.external_write",
                        arguments={"destination": "demo", "content": "restart-safe"},
                        call_id="durable-approval",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            )
        ]
    )
    async with build(
        settings=database_settings(), storage="postgres", script=proposed
    ) as composition:
        run_id = await composition.runs.submit("record a durable external write")
        first_worker = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=composition.clock,
            worker_id="approval-worker-before-restart",
        )
        assert await first_worker.run_once()
        parked = await composition.runs.get(run_id)
        assert parked.status is RunStatus.WAITING_FOR_APPROVAL
        assert parked.lease_owner is None
        assert parked.lease_expires_at is None

    resumed_script = FakeModelScript(
        turns=[ScriptedTurn(text="done", stop_reason=StopReason.END_TURN)]
    )
    async with build(
        settings=database_settings(), storage="postgres", script=resumed_script
    ) as composition:
        approval = (await composition.approvals.list_pending(run_id=run_id))[0]
        await composition.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        resumed_worker = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=composition.clock,
            worker_id="approval-worker-after-restart",
        )
        assert await resumed_worker.run_once()
        completed = await composition.runs.get(run_id)
        async with composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(
                run_id,
                Principal(tenant_id="local", principal_id="local-user"),
            )

    assert completed.status is RunStatus.COMPLETED
    assert completed.final_message == "done"
    assert len(invocations) == 1
    assert invocations[0].status is ToolInvocationStatus.SUCCEEDED
    assert invocations[0].structured_result is not None
    assert invocations[0].structured_result["byte_count"] == 12
