"""PostgreSQL journey for conversationally resuming a paused schedule."""

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from typing import Any, cast
from uuid import UUID

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import Composition, build
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn
from agent_core.domain.runs import RunStatus
from agent_core.domain.schedules import SchedulePauseReason, ScheduleState, WeeklyCadence
from agent_core.runtime.worker import DurableWorker
from agent_core.tools.registry import RegisteredTool
from agent_core.tools.schedule_create import ScheduleCreateTool
from tests.contract.support import tool_context
from tests.integration.m2_support import database_settings

NOW = datetime(2026, 9, 2, 17, tzinfo=UTC)
PLACEHOLDER_ID = UUID("00000000-0000-0000-0000-000000002399")


async def _run_worker(composition: Composition, worker_id: str) -> None:
    worker = DurableWorker(
        uow_factory=composition.uow_factory,
        executor=composition.executor,
        clock=composition.clock,
        worker_id=worker_id,
    )
    assert await worker.run_once()


async def test_paused_weekday_briefing_is_discovered_approved_and_resumed() -> None:
    resume_call = ScriptedToolCall(
        name="schedule.resume",
        arguments={"schedule_id": str(PLACEHOLDER_ID), "expected_revision": 1},
        call_id="resume-technology-briefing",
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="schedule.list",
                        arguments={},
                        call_id="find-technology-briefing",
                    )
                ]
            ),
            ScriptedTurn(tool_calls=[resume_call]),
            ScriptedTurn(text="Your Mon-Fri technology briefing is active again."),
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
        enabled_tools=["schedule.list", "schedule.resume"],
    ) as composition:
        registered = cast(
            RegisteredTool,
            composition.tool_pipeline._registry.get("schedule.create"),
        )
        create = cast(ScheduleCreateTool, registered.implementation)
        created_result = await create.execute(
            {
                "title": "Mon-Fri technology briefing",
                "instruction": "Summarize important technology news.",
                "cadence": {
                    "kind": "WEEKLY",
                    "local_time": "08:00:00",
                    "weekdays": [1, 2, 3, 4, 5],
                    "timezone": "America/Los_Angeles",
                },
            },
            replace(
                tool_context(),
                principal=composition.principal,
                tenant_id=composition.principal.tenant_id,
                idempotency_key="technology-briefing",
            ),
        )
        assert created_result.ok is True
        assert created_result.structured is not None
        schedule_id = UUID(created_result.structured["schedule_id"])
        paused = await composition.schedules.pause(composition.principal, schedule_id, 1)
        assert paused.schedule.state is ScheduleState.PAUSED
        assert paused.schedule.pause_reason is SchedulePauseReason.USER

        clock = composition.clock
        assert isinstance(clock, FixedClock)
        clock.advance(timedelta(days=14))
        resume_call.arguments = {
            "schedule_id": str(schedule_id),
            "expected_revision": 1,
        }

        session_id = await composition.sessions.create()
        run_id = await composition.runs.submit(
            "Resume my Mon-Fri technology briefing.",
            session_id,
        )
        await _run_worker(composition, "lifecycle-conversation-worker")
        waiting = await composition.runs.get(run_id)
        [approval] = await composition.approvals.list_pending(run_id=run_id)

        assert waiting.status is RunStatus.WAITING_FOR_APPROVAL
        assert approval.tool_name == "schedule.resume"
        assert approval.required_scopes == {"schedule.write"}
        assert approval.arguments == resume_call.arguments

        await composition.approvals.resolve(
            approval.id,
            ApprovalResolutionType.APPROVE_ONCE,
        )
        await _run_worker(composition, "approved-lifecycle-worker")
        completed = await composition.runs.get(run_id)
        resumed = await composition.schedules.get(composition.principal, schedule_id)

    assert completed.status is RunStatus.COMPLETED
    assert completed.final_message == "Your Mon-Fri technology briefing is active again."
    assert resumed.schedule.state is ScheduleState.ACTIVE
    assert resumed.schedule.pause_reason is None
    assert resumed.schedule.next_fire_at is not None
    assert resumed.schedule.next_fire_at > clock.now()


async def test_weekday_briefing_is_discovered_approved_and_updated() -> None:
    update_arguments: dict[str, Any] = {
        "schedule_id": str(PLACEHOLDER_ID),
        "expected_revision": 1,
        "instruction": "Summarize security and infrastructure news with source links.",
        "cadence": {
            "kind": "WEEKLY",
            "local_time": "09:00:00",
            "weekdays": [1, 3, 5],
            "timezone": "America/Los_Angeles",
        },
    }
    update_call = ScriptedToolCall(
        name="schedule.update",
        arguments=update_arguments,
        call_id="update-technology-briefing",
    )
    scripted_update_arguments = cast(dict[str, Any], update_call.arguments)
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="schedule.list",
                        arguments={},
                        call_id="find-technology-briefing",
                    )
                ]
            ),
            ScriptedTurn(tool_calls=[update_call]),
            ScriptedTurn(
                text="Your briefing now covers security every Monday, Wednesday, and Friday."
            ),
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
        enabled_tools=["schedule.list", "schedule.update"],
    ) as composition:
        registered = cast(
            RegisteredTool,
            composition.tool_pipeline._registry.get("schedule.create"),
        )
        create = cast(ScheduleCreateTool, registered.implementation)
        created_result = await create.execute(
            {
                "title": "Technology briefing",
                "instruction": "Summarize important technology news.",
                "cadence": {
                    "kind": "WEEKLY",
                    "local_time": "08:00:00",
                    "weekdays": [1, 2, 3, 4, 5],
                    "timezone": "America/Los_Angeles",
                },
            },
            replace(
                tool_context(),
                principal=composition.principal,
                tenant_id=composition.principal.tenant_id,
                idempotency_key="technology-briefing-update-source",
            ),
        )
        assert created_result.ok is True
        assert created_result.structured is not None
        schedule_id = UUID(created_result.structured["schedule_id"])
        scripted_update_arguments["schedule_id"] = str(schedule_id)

        session_id = await composition.sessions.create()
        run_id = await composition.runs.submit(
            "Change my technology briefing to security on Monday, Wednesday, and Friday.",
            session_id,
        )
        await _run_worker(composition, "update-conversation-worker")
        waiting = await composition.runs.get(run_id)
        [approval] = await composition.approvals.list_pending(run_id=run_id)

        assert waiting.status is RunStatus.WAITING_FOR_APPROVAL
        assert approval.tool_name == "schedule.update"
        assert approval.required_scopes == {"schedule.write"}
        assert approval.arguments == scripted_update_arguments

        await composition.approvals.resolve(
            approval.id,
            ApprovalResolutionType.APPROVE_ONCE,
        )
        await _run_worker(composition, "approved-update-worker")
        completed = await composition.runs.get(run_id)
        updated = await composition.schedules.get(composition.principal, schedule_id)
        async with composition.uow_factory() as uow:
            original = await uow.schedules.get_revision(schedule_id, 1, composition.principal)

    assert completed.status is RunStatus.COMPLETED
    assert updated.schedule.current_revision == 2
    assert updated.schedule.next_fire_at is not None
    assert updated.schedule.next_fire_at > NOW
    assert original.instruction == "Summarize important technology news."
    assert updated.revision.instruction == scripted_update_arguments["instruction"]
    assert updated.revision.cadence == WeeklyCadence(
        local_time=time(9),
        weekdays=(1, 3, 5),
        timezone="America/Los_Angeles",
    )
