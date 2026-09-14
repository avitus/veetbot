"""PostgreSQL journey for creating a schedule after a user clarification.

Worker heartbeat waits use real time while application decisions use the fixed
date. FixedClock.sleep advances immediately, so using it for concurrent heartbeats
can exhaust a tool deadline during database I/O. The delayed-start case reproduces
that false timeout without relaxing approval, persistence, or deadline checks.
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from agent_core.adapters.determinism import SystemClock
from agent_core.adapters.persistence.repositories import PostgresToolInvocationRepository
from agent_core.bootstrap import Composition, build
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    ToolCallItem,
    ToolResultItem,
)
from agent_core.domain.persistence import WorkerLease
from agent_core.domain.runs import RunStatus
from agent_core.domain.tools import ToolInvocation, ToolInvocationStatus
from agent_core.domain.views import TextContentBlock
from agent_core.runtime.worker import DurableWorker
from tests.integration.m2_support import database_settings

NOW = datetime(2026, 8, 25, 20, tzinfo=UTC)
FIRE_AT = datetime(2026, 8, 26, 2, tzinfo=UTC)


async def _run_worker(composition: Composition, worker_id: str) -> None:
    """Run one claim without advancing domain time on every heartbeat wait."""

    worker = DurableWorker(
        uow_factory=composition.uow_factory,
        executor=composition.executor,
        clock=SystemClock(),
        worker_id=worker_id,
    )
    assert await worker.run_once()


@pytest.mark.parametrize("database_delay", [0.0, 0.05])
async def test_clarified_reminder_resumes_once_and_creates_schedule(
    monkeypatch: pytest.MonkeyPatch, database_delay: float
) -> None:
    """Clarification and approval persist one reminder despite database latency."""

    transition = PostgresToolInvocationRepository.transition

    async def delayed_start(
        repository: PostgresToolInvocationRepository,
        invocation_id: UUID,
        expected_status: ToolInvocationStatus,
        invocation: ToolInvocation,
        *,
        lease: WorkerLease | None = None,
    ) -> ToolInvocation:
        if (
            database_delay
            and invocation.tool_name == "schedule.create"
            and expected_status is ToolInvocationStatus.AUTHORIZED
            and invocation.status is ToolInvocationStatus.RUNNING
        ):
            await asyncio.sleep(database_delay)
        return await transition(repository, invocation_id, expected_status, invocation, lease=lease)

    monkeypatch.setattr(PostgresToolInvocationRepository, "transition", delayed_start)

    arguments = {
        "title": "Throw the ball for Marzipan",
        "instruction": "Remind me to throw the ball for Marzipan.",
        "at": FIRE_AT.isoformat(),
    }
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="conversation.ask_user",
                        arguments={
                            "question": ("Do you mean 7:00 PM today (August 25) in Pacific Time?")
                        },
                        call_id="clarify-reminder-time",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="schedule.create",
                        arguments=arguments,
                        call_id="create-marzipan-reminder",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="I scheduled the reminder."),
        ]
    )
    settings = replace(
        database_settings(),
        schedule_api_enabled=True,
        schedule_worker_enabled=True,
    )

    async with build(
        settings=settings,
        storage="postgres",
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=["conversation.ask_user", "schedule.create"],
    ) as composition:
        session_id = await composition.sessions.create()
        run_id = await composition.runs.submit(
            "Remind me at 7pm to throw the ball for Marzipan.",
            session_id,
        )
        await _run_worker(composition, "clarification-worker")

        waiting = await composition.runs.get(run_id)
        events = await composition.runs.events(run_id)
        waiting_event = next(
            event for event in events if event.event_type == "run.waiting_for_user"
        )
        await composition.services.runs.deliver_input(
            composition.principal,
            run_id,
            [TextContentBlock(text="Yes.")],
            UUID(str(waiting_event.payload["question_id"])),
        )
        await _run_worker(composition, "resumed-clarification-worker")

        resumed = await composition.runs.get(run_id)
        approvals = await composition.approvals.list_pending(run_id=run_id)
        async with composition.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(run_id)

        assert waiting.status is RunStatus.WAITING_FOR_USER
        assert resumed.status is RunStatus.WAITING_FOR_APPROVAL
        assert len(approvals) == 1
        assert approvals[0].tool_name == "schedule.create"
        assert checkpoint is not None
        assert (
            sum(
                isinstance(item, ToolCallItem) and item.call_id == "clarify-reminder-time"
                for item in checkpoint.conversation
            )
            == 1
        )
        assert (
            sum(
                isinstance(item, ToolResultItem) and item.call_id == "clarify-reminder-time"
                for item in checkpoint.conversation
            )
            == 1
        )

        await composition.approvals.resolve(
            approvals[0].id,
            ApprovalResolutionType.APPROVE_ONCE,
        )
        await _run_worker(composition, "approved-reminder-worker")
        completed = await composition.runs.get(run_id)
        schedules = await composition.schedules.list(composition.principal, 10, None)
        async with composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)

        creation = [item for item in invocations if item.tool_name == "schedule.create"]
        assert len(creation) == 1
        assert creation[0].result_item is not None
        assert not creation[0].result_item.is_error, creation[0].outcome
        assert composition.clock.now() == NOW

    assert completed.status is RunStatus.COMPLETED
    assert completed.final_message == "I scheduled the reminder."
    assert len(schedules.items) == 1
    assert schedules.items[0].schedule.next_fire_at == FIRE_AT
    assert schedules.items[0].revision.title == arguments["title"]
