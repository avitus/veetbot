"""ADR-0129 and ADR-0302: PostgreSQL stores approving resolutions as approved."""

from __future__ import annotations

from agent_core.bootstrap import build
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.runs import RunStatus
from agent_core.runtime.worker import DurableWorker
from tests.contract.test_approval_repository_contract import (
    REQUEST_OFFER,
    assert_approve_for_request_keeps_its_offer,
    assert_approve_for_task_resolves_to_approved,
)
from tests.integration.m2_support import database_settings


async def test_approve_for_task_resolves_to_approved() -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="demo.external_write",
                        arguments={"destination": "demo", "content": "task grant"},
                        call_id="approve-for-task",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            )
        ]
    )
    async with build(
        settings=database_settings(), storage="postgres", script=script
    ) as composition:
        run_id = await composition.runs.submit("record an external write")
        worker = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=composition.clock,
            worker_id="approve-for-task-worker",
        )
        assert await worker.run_once()
        assert (await composition.runs.get(run_id)).status is RunStatus.WAITING_FOR_APPROVAL
        approval = (await composition.approvals.list_pending(run_id=run_id))[0]

        async with composition.uow_factory() as uow:
            await assert_approve_for_task_resolves_to_approved(
                uow.approvals, approval.id, composition.principal
            )


async def test_approve_for_request_keeps_its_offer() -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="demo.external_write",
                        arguments={"destination": "demo", "content": "request grant"},
                        call_id="approve-for-request",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            )
        ]
    )
    async with build(
        settings=database_settings(), storage="postgres", script=script
    ) as composition:
        run_id = await composition.runs.submit("record an external write")
        worker = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=composition.clock,
            worker_id="approve-for-request-worker",
        )
        assert await worker.run_once()
        pending = (await composition.approvals.list_pending(run_id=run_id))[0]

        # Store the same pending approval again, now carrying a request offer.
        async with composition.uow_factory() as uow:
            await uow.approvals.discard_pending(pending.id)
            await uow.approvals.create(
                pending.model_copy(update={"request_grant_offer": REQUEST_OFFER})
            )
        async with composition.uow_factory() as uow:
            await assert_approve_for_request_keeps_its_offer(
                uow.approvals, pending.id, composition.principal
            )
