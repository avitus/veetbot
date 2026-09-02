"""Milestone 23 conversational schedule lifecycle gates."""

import json
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
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
    ScheduleRevision,
    ScheduleState,
)
from agent_core.domain.tools import ToolFailureKind
from agent_core.scheduling.worker import ScheduleWorker
from agent_core.tools.registry import RegisteredTool
from tests.contract.support import tool_context
from tests.integration.m2_support import memory_settings

NOW = datetime(2026, 9, 2, 17, tzinfo=UTC)
SCHEDULE_ID = UUID("00000000-0000-0000-0000-000000002300")
OTHER_SCHEDULE_ID = UUID("00000000-0000-0000-0000-000000002301")
LIFECYCLE_NAMES = (
    "schedule.list",
    "schedule.pause",
    "schedule.resume",
    "schedule.cancel",
)


def _enabled_settings() -> Settings:
    return replace(
        memory_settings(),
        schedule_api_enabled=True,
        schedule_worker_enabled=True,
    )


async def _seed_schedule(
    composition: Composition,
    *,
    schedule_id: UUID = SCHEDULE_ID,
    title: str = "Mon-Fri technology briefing",
    next_fire_at: datetime | None = None,
    local_time: time = time(17, 1),
) -> None:
    session_id = await composition.sessions.create()
    async with composition.uow_factory() as uow:
        session = await uow.sessions.get(session_id, composition.principal)
        schedule = Schedule(
            id=schedule_id,
            tenant_id=composition.principal.tenant_id,
            principal_id=composition.principal.principal_id,
            state=ScheduleState.ACTIVE,
            current_revision=1,
            next_fire_at=next_fire_at or NOW + timedelta(minutes=1),
            created_at=NOW,
            updated_at=NOW,
        )
        revision = ScheduleRevision(
            schedule_id=schedule_id,
            revision=1,
            title=title,
            instruction="Summarize the day's important technology news.",
            agent_id=session.agent_id,
            agent_version=session.agent_version,
            policy_profile="default",
            requested_scopes=frozenset(),
            limits=RunLimits(
                max_steps=4,
                max_model_calls=4,
                max_tool_calls=4,
                max_cost=Decimal("1"),
            ),
            run_timeout_seconds=60,
            cadence=DailyCadence(local_time=local_time, timezone="UTC"),
            timezone="UTC",
            misfire_grace_seconds=60,
            max_consecutive_failures=1,
            created_by_principal_id=composition.principal.principal_id,
            created_at=NOW,
        )
        await uow.schedules.create(schedule, revision)


def _implementation(composition: Composition, name: str) -> Any:
    registered = cast(RegisteredTool, composition.tool_pipeline._registry.get(name))
    return registered.implementation


async def test_schedule_lifecycle_tools_are_feature_gated_and_classified() -> None:
    async with build(
        settings=memory_settings(),
        script=FakeModelScript(turns=[ScriptedTurn(text="ready")]),
    ) as composition:
        for name in LIFECYCLE_NAMES:
            with pytest.raises(NotFoundError):
                composition.tool_pipeline._registry.get(name)
        run_id = await composition.runs.submit("ready?")
        run = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            disabled_agent = await uow.agents.get_version(run.agent_id, run.agent_version)

    async with build(
        settings=_enabled_settings(),
        script=FakeModelScript(turns=[ScriptedTurn(text="ready")]),
    ) as composition:
        specs = {
            name: composition.tool_pipeline._registry.get(name).spec for name in LIFECYCLE_NAMES
        }
        run_id = await composition.runs.submit("ready?")
        run = await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            enabled_agent = await uow.agents.get_version(run.agent_id, run.agent_version)

    assert set(LIFECYCLE_NAMES).isdisjoint(disabled_agent.enabled_tools)
    assert set(LIFECYCLE_NAMES) <= set(enabled_agent.enabled_tools)
    assert specs["schedule.list"].required_scopes == {"schedule.read"}
    assert specs["schedule.list"].side_effect is SideEffectClass.NONE
    assert specs["schedule.list"].risk is RiskLevel.LOW
    assert specs["schedule.list"].idempotency is IdempotencyClass.READ_ONLY
    assert specs["schedule.list"].allow_parallel is True
    for name in ("schedule.pause", "schedule.resume"):
        assert specs[name].required_scopes == {"schedule.write"}
        assert specs[name].side_effect is SideEffectClass.EXTERNAL_WRITE
        assert specs[name].risk is RiskLevel.HIGH
        assert specs[name].idempotency is IdempotencyClass.IDEMPOTENT
        assert specs[name].allow_parallel is False
    assert specs["schedule.cancel"].required_scopes == {"schedule.cancel"}
    assert specs["schedule.cancel"].side_effect is SideEffectClass.EXTERNAL_DELETE
    assert specs["schedule.cancel"].risk is RiskLevel.HIGH
    assert specs["schedule.cancel"].idempotency is IdempotencyClass.IDEMPOTENT
    assert specs["schedule.cancel"].allow_parallel is False


async def test_schedule_list_is_bounded_paginated_and_summary_only() -> None:
    async with build(
        settings=_enabled_settings(),
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=list(LIFECYCLE_NAMES),
    ) as composition:
        for index in range(51):
            await _seed_schedule(
                composition,
                schedule_id=UUID(int=SCHEDULE_ID.int + index),
                title=f"Briefing {index:02d}",
                next_fire_at=NOW + timedelta(days=index + 1),
            )
        tool = _implementation(composition, "schedule.list")
        context = replace(tool_context(), principal=composition.principal)
        first = await tool.execute({"limit": 50}, context)
        assert first.ok is True
        assert first.structured is not None
        second = await tool.execute(
            {"limit": 50, "cursor": first.structured["next_cursor"]}, context
        )

    assert len(first.structured["items"]) == 50
    assert first.structured["next_cursor"] is not None
    assert second.ok is True
    assert second.structured is not None
    assert len(second.structured["items"]) == 1
    allowed = {
        "schedule_id",
        "title",
        "state",
        "pause_reason",
        "current_revision",
        "next_fire_at",
        "cadence",
    }
    assert all(set(item) == allowed for item in first.structured["items"])
    encoded = json.dumps(first.structured)
    assert "Summarize the day's important technology news" not in encoded
    assert "requested_scopes" not in encoded
    assert "policy_profile" not in encoded
    assert "principal_id" not in encoded


async def test_schedule_pause_and_resume_run_through_approval_without_backfill() -> None:
    arguments = {"schedule_id": str(SCHEDULE_ID), "expected_revision": 1}
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(name="schedule.list", arguments={}, call_id="find-briefing")
                ]
            ),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(name="schedule.pause", arguments=arguments, call_id="pause")
                ]
            ),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(name="schedule.resume", arguments=arguments, call_id="resume")
                ]
            ),
            ScriptedTurn(text="The Mon-Fri technology briefing is active again."),
        ]
    )
    async with build(
        settings=_enabled_settings(),
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=list(LIFECYCLE_NAMES),
    ) as composition:
        await _seed_schedule(composition)
        run_id = await composition.runs.submit("Pause and then resume my technology briefing.")
        [pause_approval] = await composition.approvals.list_pending(run_id=run_id)
        assert pause_approval.tool_name == "schedule.pause"
        assert pause_approval.required_scopes == {"schedule.write"}
        assert pause_approval.arguments == arguments

        await composition.approvals.resolve(
            pause_approval.id,
            ApprovalResolutionType.APPROVE_ONCE,
        )
        [resume_approval] = await composition.approvals.list_pending(run_id=run_id)
        paused = await composition.schedules.get(composition.principal, SCHEDULE_ID)
        assert paused.schedule.state is ScheduleState.PAUSED
        assert paused.schedule.pause_reason is SchedulePauseReason.USER
        assert paused.schedule.next_fire_at is None
        assert resume_approval.tool_name == "schedule.resume"
        assert resume_approval.required_scopes == {"schedule.write"}

        clock = composition.clock
        assert isinstance(clock, FixedClock)
        clock.advance(timedelta(days=30))
        await composition.approvals.resolve(
            resume_approval.id,
            ApprovalResolutionType.APPROVE_ONCE,
        )
        completed = await composition.runs.wait_terminal(run_id)
        resumed = await composition.schedules.get(composition.principal, SCHEDULE_ID)
        async with composition.uow_factory() as uow:
            pause_events = await uow.process_events.list("schedule.paused")
            resume_events = await uow.process_events.list("schedule.resumed")

    assert completed.status is RunStatus.COMPLETED
    assert resumed.schedule.state is ScheduleState.ACTIVE
    assert resumed.schedule.pause_reason is None
    assert resumed.schedule.next_fire_at is not None
    assert resumed.schedule.next_fire_at > clock.now()
    assert len(pause_events) == 1
    assert len(resume_events) == 1


async def test_schedule_cancel_runs_through_approval_and_preserves_history() -> None:
    arguments = {"schedule_id": str(SCHEDULE_ID), "expected_revision": 1}
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(name="schedule.cancel", arguments=arguments, call_id="delete")
                ]
            ),
            ScriptedTurn(text="I cancelled the briefing and retained its history."),
        ]
    )
    async with build(
        settings=_enabled_settings(),
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=list(LIFECYCLE_NAMES),
    ) as composition:
        await _seed_schedule(composition)
        clock = composition.clock
        assert isinstance(clock, FixedClock)
        clock.advance(timedelta(minutes=1))
        schedule_worker = cast(ScheduleWorker, composition.schedule_worker_factory())
        assert await schedule_worker.run_once() == 1
        before = await composition.schedules.list_occurrences(
            composition.principal,
            SCHEDULE_ID,
            limit=10,
            cursor=None,
        )
        assert len(before.items) == 1
        assert before.items[0].run_id is not None
        async with composition.uow_factory() as uow:
            linked_before = await uow.runs.get(before.items[0].run_id, composition.principal)

        run_id = await composition.runs.submit("Delete my technology briefing.")
        [approval] = await composition.approvals.list_pending(run_id=run_id)
        assert approval.tool_name == "schedule.cancel"
        assert approval.required_scopes == {"schedule.cancel"}
        await composition.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        await composition.runs.wait_terminal(run_id)

        cancelled = await composition.schedules.get(composition.principal, SCHEDULE_ID)
        after = await composition.schedules.list_occurrences(
            composition.principal,
            SCHEDULE_ID,
            limit=10,
            cursor=None,
        )
        async with composition.uow_factory() as uow:
            linked_after = await uow.runs.get(before.items[0].run_id, composition.principal)

    assert cancelled.schedule.state is ScheduleState.CANCELLED
    assert cancelled.schedule.next_fire_at is None
    assert after.items == before.items
    assert linked_after.status is linked_before.status


@pytest.mark.parametrize(
    ("tool_name", "arguments", "required_scope"),
    [
        ("schedule.list", {}, "schedule.read"),
        (
            "schedule.pause",
            {"schedule_id": str(SCHEDULE_ID), "expected_revision": 1},
            "schedule.write",
        ),
        (
            "schedule.resume",
            {"schedule_id": str(SCHEDULE_ID), "expected_revision": 1},
            "schedule.write",
        ),
        (
            "schedule.cancel",
            {"schedule_id": str(SCHEDULE_ID), "expected_revision": 1},
            "schedule.cancel",
        ),
    ],
)
async def test_schedule_lifecycle_tools_require_exact_scopes_before_execution(
    tool_name: str,
    arguments: dict[str, Any],
    required_scope: str,
) -> None:
    principal = Principal(
        tenant_id="local",
        principal_id="local-user",
        roles={"user"},
        scopes={"approval.read"},
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(tool_calls=[ScriptedToolCall(name=tool_name, arguments=arguments)]),
            ScriptedTurn(text="The operation was denied."),
        ]
    )
    async with build(
        settings=_enabled_settings(),
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=[tool_name],
        principal=principal,
    ) as composition:
        assert composition.tool_pipeline._registry.get(tool_name).spec.required_scopes == {
            required_scope
        }
        run_id = await composition.runs.submit("Change a schedule without authority.")
        completed = await composition.runs.wait_terminal(run_id)
        approvals = await composition.approvals.list_pending(run_id=run_id)
        run = await composition.runs.get(run_id)
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(run.session_id, 0, principal)

    denials = [event for event in events if event.event_type == "tool.call.denied"]
    assert completed.status is RunStatus.COMPLETED
    assert approvals == []
    assert len(denials) == 1
    assert denials[0].payload["reason_code"] == "policy.scope.missing"


async def test_schedule_lifecycle_validation_and_conflicts_fail_closed() -> None:
    async with build(
        settings=_enabled_settings(),
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=list(LIFECYCLE_NAMES),
    ) as composition:
        await _seed_schedule(composition)
        await _seed_schedule(composition, schedule_id=OTHER_SCHEDULE_ID, title="Other briefing")
        context = replace(tool_context(), principal=composition.principal)
        pause = _implementation(composition, "schedule.pause")

        malformed = await pause.execute(
            {"schedule_id": "not-a-uuid", "expected_revision": 1},
            context,
        )
        unknown = await pause.execute(
            {"schedule_id": str(UUID(int=9999)), "expected_revision": 1},
            context,
        )
        stale = await pause.execute(
            {"schedule_id": str(SCHEDULE_ID), "expected_revision": 2},
            context,
        )
        await composition.schedules.cancel(composition.principal, SCHEDULE_ID, 1)
        terminal = await pause.execute(
            {"schedule_id": str(SCHEDULE_ID), "expected_revision": 1},
            context,
        )
        other = await composition.schedules.get(composition.principal, OTHER_SCHEDULE_ID)

    assert malformed.failure is not None
    assert malformed.failure.kind is ToolFailureKind.INVALID_ARGUMENTS
    assert malformed.failure.reason_code == "schedule.id_invalid"
    assert unknown.failure is not None
    assert unknown.failure.kind is ToolFailureKind.NOT_FOUND
    assert unknown.failure.reason_code == "schedule.not_found"
    assert stale.failure is not None
    assert stale.failure.kind is ToolFailureKind.INVALID_ARGUMENTS
    assert stale.failure.reason_code == "schedule.revision_conflict"
    assert terminal.failure is not None
    assert terminal.failure.kind is ToolFailureKind.INVALID_ARGUMENTS
    assert terminal.failure.reason_code == "schedule.terminal"
    assert other.schedule.state is ScheduleState.ACTIVE


async def test_schedule_lifecycle_retries_are_state_idempotent() -> None:
    arguments = {"schedule_id": str(SCHEDULE_ID), "expected_revision": 1}
    async with build(
        settings=_enabled_settings(),
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=list(LIFECYCLE_NAMES),
    ) as composition:
        await _seed_schedule(composition)
        context = replace(tool_context(), principal=composition.principal)
        for name in ("schedule.pause", "schedule.resume", "schedule.cancel"):
            tool = _implementation(composition, name)
            first = await tool.execute(arguments, context)
            second = await tool.execute(arguments, context)
            assert first.ok is True
            assert second.ok is True
            assert second.structured == first.structured
        async with composition.uow_factory() as uow:
            paused = await uow.process_events.list("schedule.paused")
            resumed = await uow.process_events.list("schedule.resumed")
            cancelled = await uow.process_events.list("schedule.cancelled")

    assert len(paused) == 1
    assert len(resumed) == 1
    assert len(cancelled) == 1
