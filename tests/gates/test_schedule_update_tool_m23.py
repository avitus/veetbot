"""Milestone 23 conversational schedule update gates."""

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import pytest

from agent_core.bootstrap import Composition, build
from agent_core.config import Settings
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.errors import NotFoundError
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass
from agent_core.domain.runs import RunLimits, RunStatus
from agent_core.domain.schedules import (
    DailyCadence,
    Schedule,
    SchedulePauseReason,
    ScheduleRecord,
    ScheduleRevision,
    ScheduleState,
    WeeklyCadence,
)
from agent_core.domain.tools import ToolFailureKind
from agent_core.tools.registry import RegisteredTool
from tests.contract.support import tool_context
from tests.integration.m2_support import memory_settings

NOW = datetime(2026, 9, 4, 17, tzinfo=UTC)
SCHEDULE_ID = UUID("00000000-0000-0000-0000-000000002302")
UPDATE_NAME = "schedule.update"


def _enabled_settings() -> Settings:
    return replace(
        memory_settings(),
        schedule_api_enabled=True,
        schedule_worker_enabled=True,
    )


async def _seed_schedule(
    composition: Composition,
    *,
    state: ScheduleState = ScheduleState.ACTIVE,
    pause_reason: SchedulePauseReason | None = None,
) -> ScheduleRecord:
    session_id = await composition.sessions.create()
    async with composition.uow_factory() as uow:
        session = await uow.sessions.get(session_id, composition.principal)
        schedule = Schedule(
            id=SCHEDULE_ID,
            tenant_id=composition.principal.tenant_id,
            principal_id=composition.principal.principal_id,
            state=state,
            pause_reason=pause_reason,
            current_revision=1,
            next_fire_at=None if state is ScheduleState.PAUSED else NOW + timedelta(minutes=1),
            created_at=NOW,
            updated_at=NOW,
        )
        revision = ScheduleRevision(
            schedule_id=SCHEDULE_ID,
            revision=1,
            title="Daily technology briefing",
            instruction="Summarize the day's important technology news.",
            agent_id=session.agent_id,
            agent_version=session.agent_version,
            policy_profile="default",
            requested_scopes=frozenset({"memory.read"}),
            limits=RunLimits(
                max_steps=4,
                max_model_calls=3,
                max_tool_calls=2,
                max_cost=Decimal("0.75"),
            ),
            run_timeout_seconds=90,
            cadence=DailyCadence(local_time=time(17, 1), timezone="UTC"),
            timezone="UTC",
            misfire_grace_seconds=120,
            max_consecutive_failures=2,
            created_by_principal_id=composition.principal.principal_id,
            created_at=NOW,
        )
        await uow.schedules.create(schedule, revision)
    return ScheduleRecord(schedule=schedule, revision=revision)


def _implementation(composition: Composition) -> Any:
    registered = cast(RegisteredTool, composition.tool_pipeline._registry.get(UPDATE_NAME))
    return registered.implementation


async def test_schedule_update_tool_is_feature_gated_and_classified() -> None:
    async with build(
        settings=memory_settings(),
        script=FakeModelScript(turns=[ScriptedTurn(text="ready")]),
    ) as composition:
        with pytest.raises(NotFoundError):
            composition.tool_pipeline._registry.get(UPDATE_NAME)
        run_id = await composition.runs.submit("ready?")
        run = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            disabled_agent = await uow.agents.get_version(run.agent_id, run.agent_version)

    async with build(
        settings=_enabled_settings(),
        script=FakeModelScript(turns=[ScriptedTurn(text="ready")]),
    ) as composition:
        spec = composition.tool_pipeline._registry.get(UPDATE_NAME).spec
        run_id = await composition.runs.submit("ready?")
        run = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            enabled_agent = await uow.agents.get_version(run.agent_id, run.agent_version)

    assert UPDATE_NAME not in disabled_agent.enabled_tools
    assert UPDATE_NAME in enabled_agent.enabled_tools
    assert spec.required_scopes == {"schedule.write"}
    assert spec.side_effect is SideEffectClass.EXTERNAL_WRITE
    assert spec.risk is RiskLevel.HIGH
    assert spec.idempotency is IdempotencyClass.CONDITIONALLY_IDEMPOTENT
    assert spec.allow_parallel is False
    assert spec.input_schema["additionalProperties"] is False
    assert set(spec.input_schema["properties"]) == {
        "schedule_id",
        "expected_revision",
        "title",
        "instruction",
        "at",
        "cadence",
    }


async def test_schedule_update_runs_through_approval_and_writes_one_revision() -> None:
    arguments = {
        "schedule_id": str(SCHEDULE_ID),
        "expected_revision": 1,
        "title": "Security briefing",
        "instruction": "Summarize only security and infrastructure news.",
        "cadence": {
            "kind": "WEEKLY",
            "local_time": "09:00:00",
            "weekdays": [1, 3, 5],
            "timezone": "America/Los_Angeles",
        },
    }
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(name="schedule.list", arguments={}, call_id="find-briefing")
                ]
            ),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(name=UPDATE_NAME, arguments=arguments, call_id="edit-briefing")
                ]
            ),
            ScriptedTurn(text="I updated the security briefing."),
        ]
    )
    async with build(
        settings=_enabled_settings(),
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=["schedule.list", UPDATE_NAME],
    ) as composition:
        original = await _seed_schedule(composition)
        run_id = await composition.runs.submit("Change my daily briefing to security on M/W/F.")
        [approval] = await composition.approvals.list_pending(run_id=run_id)
        assert approval.tool_name == UPDATE_NAME
        assert approval.required_scopes == {"schedule.write"}
        assert approval.arguments == arguments
        assert str(SCHEDULE_ID) in approval.action_summary
        assert "revision 1" in approval.action_summary

        await composition.approvals.resolve(
            approval.id,
            ApprovalResolutionType.APPROVE_ONCE,
        )
        completed = await composition.runs.wait_terminal(run_id)
        updated = await composition.schedules.get(composition.principal, SCHEDULE_ID)
        async with composition.uow_factory() as uow:
            revision_one = await uow.schedules.get_revision(SCHEDULE_ID, 1, composition.principal)
            revision_two = await uow.schedules.get_revision(SCHEDULE_ID, 2, composition.principal)
            events = await uow.process_events.list("schedule.updated")

    assert completed.status is RunStatus.COMPLETED
    assert updated.schedule.current_revision == 2
    assert updated.schedule.next_fire_at is not None
    assert updated.schedule.next_fire_at > NOW
    assert revision_one == original.revision
    assert revision_two.title == arguments["title"]
    assert revision_two.instruction == arguments["instruction"]
    assert revision_two.cadence == WeeklyCadence(
        local_time=time(9),
        weekdays=(1, 3, 5),
        timezone="America/Los_Angeles",
    )
    assert len(events) == 1


async def test_schedule_update_preserves_hidden_fields_and_paused_state() -> None:
    async with build(
        settings=_enabled_settings(),
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=[UPDATE_NAME],
    ) as composition:
        original = await _seed_schedule(
            composition,
            state=ScheduleState.PAUSED,
            pause_reason=SchedulePauseReason.USER,
        )
        result = await _implementation(composition).execute(
            {
                "schedule_id": str(SCHEDULE_ID),
                "expected_revision": 1,
                "instruction": "Summarize security news with source links.",
            },
            replace(
                tool_context(),
                principal=composition.principal,
                idempotency_key="preserve-hidden-fields",
            ),
        )
        updated = await composition.schedules.get(composition.principal, SCHEDULE_ID)

    assert result.ok is True
    assert updated.schedule.state is ScheduleState.PAUSED
    assert updated.schedule.pause_reason is SchedulePauseReason.USER
    assert updated.schedule.next_fire_at is None
    assert updated.revision.instruction == "Summarize security news with source links."
    assert updated.revision.title == original.revision.title
    assert updated.revision.cadence == original.revision.cadence
    assert updated.revision.agent_id == original.revision.agent_id
    assert updated.revision.agent_version == original.revision.agent_version
    assert updated.revision.policy_profile == original.revision.policy_profile
    assert updated.revision.requested_scopes == original.revision.requested_scopes
    assert updated.revision.limits == original.revision.limits
    assert updated.revision.run_timeout_seconds == original.revision.run_timeout_seconds
    assert updated.revision.misfire_grace_seconds == original.revision.misfire_grace_seconds
    assert updated.revision.max_consecutive_failures == original.revision.max_consecutive_failures


async def test_schedule_update_authorization_and_validation_fail_closed() -> None:
    unauthorized = Principal(
        tenant_id="local",
        principal_id="local-user",
        roles={"user"},
        scopes={"schedule.read", "approval.read"},
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name=UPDATE_NAME,
                        arguments={
                            "schedule_id": str(SCHEDULE_ID),
                            "expected_revision": 1,
                            "title": "Unauthorized edit",
                        },
                    )
                ]
            ),
            ScriptedTurn(text="The edit was denied."),
        ]
    )
    async with build(
        settings=_enabled_settings(),
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=[UPDATE_NAME],
        principal=unauthorized,
    ) as composition:
        await _seed_schedule(composition)
        run_id = await composition.runs.submit("Change the briefing without authority.")
        completed = await composition.runs.wait_terminal(run_id)
        approvals = await composition.approvals.list_pending(run_id=run_id)
        unchanged = await composition.schedules.get(unauthorized, SCHEDULE_ID)
        async with composition.uow_factory() as uow:
            update_events = await uow.process_events.list("schedule.updated")

    assert completed.status is RunStatus.COMPLETED
    assert approvals == []
    assert unchanged.schedule.current_revision == 1
    assert update_events == []

    async with build(
        settings=_enabled_settings(),
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=[UPDATE_NAME],
    ) as composition:
        await _seed_schedule(composition)
        tool = _implementation(composition)
        context = replace(
            tool_context(),
            principal=composition.principal,
            idempotency_key="invalid-update",
        )
        empty = await tool.execute(
            {"schedule_id": str(SCHEDULE_ID), "expected_revision": 1}, context
        )
        stale = await tool.execute(
            {
                "schedule_id": str(SCHEDULE_ID),
                "expected_revision": 2,
                "title": "Stale edit",
            },
            replace(context, idempotency_key="stale-update"),
        )
        secret = await tool.execute(
            {
                "schedule_id": str(SCHEDULE_ID),
                "expected_revision": 1,
                "instruction": "Authorization: " + "Bearer secret-value",
            },
            replace(context, idempotency_key="secret-update"),
        )
        await composition.schedules.cancel(composition.principal, SCHEDULE_ID, 1)
        terminal = await tool.execute(
            {
                "schedule_id": str(SCHEDULE_ID),
                "expected_revision": 1,
                "title": "Terminal edit",
            },
            replace(context, idempotency_key="terminal-update"),
        )
        final = await composition.schedules.get(composition.principal, SCHEDULE_ID)
        async with composition.uow_factory() as uow:
            update_events = await uow.process_events.list("schedule.updated")

    assert empty.failure is not None
    assert empty.failure.reason_code == "schedule.update_empty"
    assert stale.failure is not None
    assert stale.failure.reason_code == "schedule.revision_conflict"
    assert secret.failure is not None
    assert secret.failure.reason_code == "schedule.instruction_contains_credential"
    assert terminal.failure is not None
    assert terminal.failure.reason_code == "schedule.terminal"
    assert final.schedule.current_revision == 1
    assert update_events == []


async def test_schedule_update_recovery_applies_at_most_once() -> None:
    arguments = {
        "schedule_id": str(SCHEDULE_ID),
        "expected_revision": 1,
        "title": "Retried security briefing",
    }
    async with build(
        settings=_enabled_settings(),
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=[UPDATE_NAME],
    ) as composition:
        await _seed_schedule(composition)
        tool = _implementation(composition)
        context = replace(
            tool_context(),
            principal=composition.principal,
            idempotency_key="update-recovery-key",
        )
        first = await tool.execute(arguments, context)
        second = await tool.execute(arguments, context)
        mismatch = await tool.execute({**arguments, "title": "Different content"}, context)
        no_op_arguments = {
            "schedule_id": str(SCHEDULE_ID),
            "expected_revision": 2,
            "title": arguments["title"],
        }
        no_op_context = replace(context, idempotency_key="update-no-op-key")
        no_op = await tool.execute(no_op_arguments, no_op_context)
        no_op_replay = await tool.execute(no_op_arguments, no_op_context)
        current = await composition.schedules.get(composition.principal, SCHEDULE_ID)
        async with composition.uow_factory() as uow:
            events = await uow.process_events.list("schedule.updated")
            with pytest.raises(NotFoundError):
                await uow.schedules.get_revision(SCHEDULE_ID, 3, composition.principal)

    assert first.ok is True
    assert first.structured is not None
    assert first.structured["replayed"] is False
    assert second.ok is True
    assert second.structured is not None
    assert second.structured["replayed"] is True
    assert second.structured["current_revision"] == 2
    assert mismatch.failure is not None
    assert mismatch.failure.kind is ToolFailureKind.INVALID_ARGUMENTS
    assert mismatch.failure.reason_code == "schedule.idempotency_mismatch"
    assert no_op.structured is not None
    assert no_op.structured["replayed"] is False
    assert no_op_replay.structured is not None
    assert no_op_replay.structured["replayed"] is True
    assert current.schedule.current_revision == 2
    assert len(events) == 1
