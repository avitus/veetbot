"""Milestone 19 conversational schedule-creation gates."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from agent_core.bootstrap import build
from agent_core.config import Settings
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.errors import NotFoundError
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass
from agent_core.domain.runs import RunStatus
from agent_core.domain.tools import ToolFailureKind
from agent_core.tools.registry import RegisteredTool
from agent_core.tools.schedule_create import ScheduleCreateTool
from tests.contract.support import tool_context
from tests.integration.m2_support import memory_settings

NOW = datetime(2026, 8, 24, 17, tzinfo=UTC)
FIRE_AT = NOW + timedelta(hours=9)
ARGUMENTS = {
    "title": "Throw the ball for Marzipan",
    "instruction": "Remind me to throw the ball for Marzipan.",
    "at": FIRE_AT.isoformat(),
}


def _enabled_settings() -> Settings:
    return replace(
        memory_settings(),
        schedule_api_enabled=True,
        schedule_worker_enabled=True,
    )


async def test_schedule_create_tool_is_feature_gated_and_classified() -> None:
    async with build(
        settings=memory_settings(),
        script=FakeModelScript(turns=[ScriptedTurn(text="ready")]),
    ) as composition:
        with pytest.raises(NotFoundError):
            composition.tool_pipeline._registry.get("schedule.create")
        run_id = await composition.runs.submit("ready?")
        run = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            disabled_agent = await uow.agents.get_version(run.agent_id, run.agent_version)

    async with build(
        settings=_enabled_settings(),
        script=FakeModelScript(turns=[ScriptedTurn(text="ready")]),
    ) as composition:
        spec = composition.tool_pipeline._registry.get("schedule.create").spec
        run_id = await composition.runs.submit("ready?")
        run = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            enabled_agent = await uow.agents.get_version(run.agent_id, run.agent_version)

    assert "schedule.create" not in disabled_agent.enabled_tools
    assert "schedule.create" in enabled_agent.enabled_tools
    assert spec.required_scopes == {"schedule.write"}
    assert spec.side_effect is SideEffectClass.EXTERNAL_WRITE
    assert spec.risk is RiskLevel.HIGH
    assert spec.idempotency is IdempotencyClass.CONDITIONALLY_IDEMPOTENT
    assert spec.allow_parallel is False


async def test_schedule_create_tool_runs_through_approval_and_persists() -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="schedule.create",
                        arguments=ARGUMENTS,
                        call_id="create-reminder",
                    )
                ]
            ),
            ScriptedTurn(text="I scheduled the reminder."),
        ]
    )
    async with build(
        settings=_enabled_settings(),
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=["schedule.create"],
    ) as composition:
        run_id = await composition.runs.submit(
            "Remind me at 7pm Pacific to throw the ball for Marzipan."
        )
        waiting = await composition.runs.get(run_id)
        [approval] = await composition.approvals.list_pending(run_id=run_id)

        assert waiting.status is RunStatus.WAITING_FOR_APPROVAL
        assert approval.tool_name == "schedule.create"
        assert approval.required_scopes == {"schedule.write"}
        assert approval.arguments == {**ARGUMENTS, "requested_scopes": []}

        await composition.approvals.resolve(
            approval.id,
            ApprovalResolutionType.APPROVE_ONCE,
        )
        completed = await composition.runs.wait_terminal(run_id)
        page = await composition.schedules.list(composition.principal, 10, None)

    assert completed.status is RunStatus.COMPLETED
    assert completed.final_message == "I scheduled the reminder."
    assert len(page.items) == 1
    record = page.items[0]
    assert record.schedule.next_fire_at == FIRE_AT
    assert record.revision.title == ARGUMENTS["title"]
    assert record.revision.instruction == ARGUMENTS["instruction"]
    assert record.revision.agent_id == completed.agent_id
    assert record.revision.agent_version == completed.agent_version
    assert record.revision.requested_scopes == frozenset()


async def test_schedule_create_tool_requires_schedule_write_scope() -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[ScriptedToolCall(name="schedule.create", arguments=ARGUMENTS)]
            ),
            ScriptedTurn(text="I could not create the schedule."),
        ]
    )
    principal = Principal(
        tenant_id="local",
        principal_id="local-user",
        roles={"user"},
        scopes=set(),
    )
    async with build(
        settings=_enabled_settings(),
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=["schedule.create"],
        principal=principal,
    ) as composition:
        run_id = await composition.runs.submit("Create the reminder without authority.")
        await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(
                (await composition.runs.get(run_id)).session_id,
                0,
                principal,
            )
        page = await composition.schedules.list(
            principal.model_copy(update={"scopes": {"schedule.read"}}, deep=True),
            10,
            None,
        )

    denials = [event for event in events if event.event_type == "tool.call.denied"]
    assert len(denials) == 1
    assert denials[0].payload["reason_code"] == "policy.scope.missing"
    assert page.items == []


@pytest.mark.parametrize(
    ("invalid_at", "reason_code"),
    [
        ((NOW - timedelta(seconds=1)).isoformat(), "schedule.no_future_occurrence"),
        (NOW.replace(tzinfo=None).isoformat(), "schedule.instant_invalid"),
    ],
)
async def test_schedule_create_tool_rejects_a_past_instant_without_state(
    invalid_at: str,
    reason_code: str,
) -> None:
    async with build(
        settings=_enabled_settings(),
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=["schedule.create"],
    ) as composition:
        registered = cast(
            RegisteredTool,
            composition.tool_pipeline._registry.get("schedule.create"),
        )
        tool = cast(ScheduleCreateTool, registered.implementation)
        context = replace(
            tool_context(),
            principal=composition.principal,
            idempotency_key="past-schedule",
        )
        result = await tool.execute(
            {**ARGUMENTS, "at": invalid_at},
            context,
        )
        page = await composition.schedules.list(composition.principal, 10, None)

    assert result.ok is False
    assert result.failure is not None
    assert result.failure.kind is ToolFailureKind.INVALID_ARGUMENTS
    assert result.failure.reason_code == reason_code
    assert page.items == []


async def test_schedule_create_tool_replays_one_idempotent_schedule() -> None:
    async with build(
        settings=_enabled_settings(),
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=["schedule.create"],
    ) as composition:
        registered = cast(
            RegisteredTool,
            composition.tool_pipeline._registry.get("schedule.create"),
        )
        tool = cast(ScheduleCreateTool, registered.implementation)
        context = replace(
            tool_context(),
            principal=composition.principal,
            idempotency_key="same-schedule-invocation",
        )
        first = await tool.execute(ARGUMENTS, context)
        second = await tool.execute(ARGUMENTS, context)
        mismatched = await tool.execute(
            {**ARGUMENTS, "instruction": "A different reminder."},
            context,
        )
        page = await composition.schedules.list(composition.principal, 10, None)

    assert first.ok is True
    assert second.ok is True
    assert first.structured is not None
    assert second.structured is not None
    assert first.structured["schedule_id"] == second.structured["schedule_id"]
    assert first.structured["replayed"] is False
    assert second.structured["replayed"] is True
    assert mismatched.ok is False
    assert mismatched.failure is not None
    assert mismatched.failure.reason_code == "schedule.idempotency_mismatch"
    assert len(page.items) == 1
