"""Milestone 20 calendar recurrence and conversational scheduling gates."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

import agent_core.domain.recurrence as recurrence_domain
import agent_core.domain.schedules as schedule_domain
from agent_core.adapters.identity import StaticSchedulePrincipalDirectory
from agent_core.adapters.schedule_admission import AllowScheduleAdmissionController
from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn
from agent_core.domain.runs import RunLimits, RunStatus
from agent_core.domain.schedules import (
    OccurrenceDisposition,
    Schedule,
    ScheduleRevision,
    ScheduleState,
)
from agent_core.domain.tools import ToolFailureKind
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from agent_core.scheduling.materializer import ScheduleMaterializer
from agent_core.tools.registry import RegisteredTool
from agent_core.tools.schedule_create import ScheduleCreateTool
from tests.contract.support import tool_context
from tests.integration.m2_support import memory_settings

NOW = datetime(2026, 8, 27, 19, tzinfo=UTC)
AGENT_ID = UUID("00000000-0000-0000-0000-000000002000")

CALENDAR_CADENCES: tuple[dict[str, object], ...] = (
    {
        "kind": "DAILY",
        "local_time": "20:00:00",
        "timezone": "UTC",
    },
    {
        "kind": "WEEKLY",
        "local_time": "10:00:00",
        "weekdays": [1, 5],
        "timezone": "UTC",
    },
    {
        "kind": "MONTHLY",
        "local_time": "09:00:00",
        "days_of_month": [31, 1],
        "last_day": False,
        "timezone": "UTC",
    },
    {
        "kind": "YEARLY",
        "local_time": "08:00:00",
        "dates": [{"month": 12, "day": 25}, {"month": 8, "day": 28}],
        "timezone": "UTC",
    },
)


def _agent() -> AgentSpec:
    return AgentSpec(
        id=AGENT_ID,
        version="1.0.0",
        name="Calendar schedule agent",
        instructions="Follow the scheduled instruction.",
        model_policy="fake",
        enabled_tools=["schedule.create"],
        policy_profile="default",
        limits=RunLimits(
            max_steps=8,
            max_model_calls=8,
            max_tool_calls=8,
            max_cost=Decimal("1"),
        ),
    )


def _definition(cadence: dict[str, object]) -> dict[str, object]:
    return {
        "title": f"{cadence['kind']} calendar task",
        "instruction": "Review the calendar task and report its result.",
        "agent_id": str(AGENT_ID),
        "agent_version": "1.0.0",
        "policy_profile": "default",
        "requested_scopes": [],
        "limits": {
            "max_steps": 4,
            "max_model_calls": 4,
            "max_tool_calls": 4,
            "max_cost": "1",
        },
        "run_timeout_seconds": 60,
        "cadence": cadence,
        "misfire_grace_seconds": 60,
        "max_consecutive_failures": 3,
    }


def _tool_arguments(cadence: dict[str, object]) -> dict[str, object]:
    return {
        "title": f"{cadence['kind']} reminder",
        "instruction": "Review the recurring reminder.",
        "cadence": cadence,
    }


def _principal() -> Principal:
    return Principal(
        tenant_id="local",
        principal_id="local-user",
        roles={"user"},
        scopes=set(PLATFORM_SCOPES),
    )


@asynccontextmanager
async def _client(composition: Composition) -> Any:
    app = create_app(
        composition.services,
        composition.settings,
        composition.principal,
        composition.new_request_id,
        composition.readiness_probe,
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        yield client


def test_calendar_cadence_values_are_closed_and_canonical() -> None:
    monthly_type = getattr(schedule_domain, "MonthlyCadence", None)
    yearly_type = getattr(schedule_domain, "YearlyCadence", None)
    month_day_type = getattr(schedule_domain, "MonthDay", None)
    assert monthly_type is not None
    assert yearly_type is not None
    assert month_day_type is not None

    monthly = monthly_type(
        local_time=time(9),
        days_of_month=(31, 1, 15),
        last_day=True,
        timezone="America/Los_Angeles",
    )
    assert monthly.days_of_month == (1, 15, 31)
    assert monthly_type.model_validate(monthly.model_dump(mode="json")) == monthly

    yearly = yearly_type(
        local_time=time(8),
        dates=(month_day_type(month=12, day=25), month_day_type(month=2, day=29)),
        timezone="UTC",
    )
    assert [(value.month, value.day) for value in yearly.dates] == [(2, 29), (12, 25)]
    assert yearly_type.model_validate(yearly.model_dump(mode="json")) == yearly

    with pytest.raises(ValidationError, match="selector"):
        monthly_type(local_time=time(9), days_of_month=(), last_day=False, timezone="UTC")
    with pytest.raises(ValidationError, match="unique"):
        monthly_type(local_time=time(9), days_of_month=(1, 1), last_day=False, timezone="UTC")
    with pytest.raises(ValidationError, match="1 through 31"):
        monthly_type(local_time=time(9), days_of_month=(32,), timezone="UTC")
    with pytest.raises(ValidationError, match="possible Gregorian date"):
        month_day_type(month=4, day=31)
    with pytest.raises(ValidationError, match="unique"):
        yearly_type(
            local_time=time(8),
            dates=(month_day_type(month=1, day=1), month_day_type(month=1, day=1)),
            timezone="UTC",
        )


def test_monthly_and_yearly_recurrence_is_deterministic() -> None:
    monthly_type = getattr(schedule_domain, "MonthlyCadence", None)
    yearly_type = getattr(schedule_domain, "YearlyCadence", None)
    month_day_type = getattr(schedule_domain, "MonthDay", None)
    assert monthly_type is not None and yearly_type is not None and month_day_type is not None

    numbered = monthly_type(local_time=time(9), days_of_month=(31,), last_day=False, timezone="UTC")
    assert recurrence_domain.RecurrenceCalculator.next_after(
        numbered, datetime(2026, 3, 31, 9, tzinfo=UTC)
    ) == datetime(2026, 5, 31, 9, tzinfo=UTC)

    month_end = monthly_type(local_time=time(9), days_of_month=(), last_day=True, timezone="UTC")
    assert recurrence_domain.RecurrenceCalculator.next_after(
        month_end, datetime(2026, 3, 31, 9, tzinfo=UTC)
    ) == datetime(2026, 4, 30, 9, tzinfo=UTC)

    leap_day = yearly_type(
        local_time=time(12),
        dates=(month_day_type(month=2, day=29),),
        timezone="UTC",
    )
    assert recurrence_domain.RecurrenceCalculator.next_after(
        leap_day, datetime(2028, 2, 29, 12, tzinfo=UTC)
    ) == datetime(2032, 2, 29, 12, tzinfo=UTC)
    assert recurrence_domain.RecurrenceCalculator.latest_at_or_before(
        leap_day, datetime(2031, 12, 31, 23, tzinfo=UTC)
    ) == datetime(2028, 2, 29, 12, tzinfo=UTC)

    spring_gap = monthly_type(
        local_time=time(2, 30),
        days_of_month=(8,),
        last_day=False,
        timezone="America/Los_Angeles",
    )
    occurrence = recurrence_domain.RecurrenceCalculator.next_after(
        spring_gap, datetime(2026, 3, 8, 9, 59, 59, tzinfo=UTC)
    )
    assert occurrence == datetime(2026, 3, 8, 10, tzinfo=UTC)
    assert occurrence.astimezone(ZoneInfo("America/Los_Angeles")).time() == time(3)

    fall_fold = yearly_type(
        local_time=time(1, 30),
        dates=(month_day_type(month=11, day=1),),
        timezone="America/Los_Angeles",
    )
    assert recurrence_domain.RecurrenceCalculator.next_after(
        fall_fold, datetime(2026, 11, 1, 8, 29, 59, tzinfo=UTC)
    ) == datetime(2026, 11, 1, 8, 30, tzinfo=UTC)
    _check_generated_calendar_recurrence()


@given(
    reference=st.datetimes(
        min_value=datetime(2025, 1, 1),
        max_value=datetime(2035, 12, 31, 23, 59, 59),
        timezones=st.just(UTC),
    ),
    hour=st.integers(min_value=0, max_value=23),
    minute=st.integers(min_value=0, max_value=59),
    timezone=st.sampled_from(
        ["UTC", "America/Los_Angeles", "Europe/London", "Australia/Lord_Howe"]
    ),
    is_monthly=st.booleans(),
    monthly_day=st.integers(min_value=1, max_value=31),
    use_last_day=st.booleans(),
    annual_date=st.sampled_from(((1, 1), (2, 29), (4, 30), (11, 1), (12, 31))),
)
def _check_generated_calendar_recurrence(
    reference: datetime,
    hour: int,
    minute: int,
    timezone: str,
    is_monthly: bool,
    monthly_day: int,
    use_last_day: bool,
    annual_date: tuple[int, int],
) -> None:
    monthly_type = schedule_domain.MonthlyCadence
    yearly_type = schedule_domain.YearlyCadence
    month_day_type = schedule_domain.MonthDay
    cadence = (
        monthly_type(
            local_time=time(hour, minute),
            days_of_month=() if use_last_day else (monthly_day,),
            last_day=use_last_day,
            timezone=timezone,
        )
        if is_monthly
        else yearly_type(
            local_time=time(hour, minute),
            dates=(month_day_type(month=annual_date[0], day=annual_date[1]),),
            timezone=timezone,
        )
    )
    first = recurrence_domain.RecurrenceCalculator.next_after(cadence, reference)
    reconstructed = recurrence_domain.RecurrenceCalculator.next_after(
        type(cadence).model_validate(cadence.model_dump(mode="json")), reference
    )
    assert first == reconstructed
    assert first is not None
    assert first > reference
    assert first.tzinfo is UTC
    assert recurrence_domain.RecurrenceCalculator.latest_at_or_before(cadence, first) == first


async def test_calendar_downtime_lookup_and_counting_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monthly_type = getattr(schedule_domain, "MonthlyCadence", None)
    yearly_type = getattr(schedule_domain, "YearlyCadence", None)
    month_day_type = getattr(schedule_domain, "MonthDay", None)
    assert monthly_type is not None and yearly_type is not None and month_day_type is not None

    calls = 0
    original = recurrence_domain._resolve_civil

    def counted(candidate_date: date, local_time: time, zone: ZoneInfo) -> datetime:
        nonlocal calls
        calls += 1
        return original(candidate_date, local_time, zone)

    monkeypatch.setattr(recurrence_domain, "_resolve_civil", counted)
    month_end = monthly_type(local_time=time(9), days_of_month=(), last_day=True, timezone="UTC")
    assert (
        recurrence_domain.RecurrenceCalculator.count_between(
            month_end,
            datetime(2000, 1, 31, 9, tzinfo=UTC),
            datetime(9998, 12, 31, 9, tzinfo=UTC),
        )
        == 95_988
    )
    assert recurrence_domain.RecurrenceCalculator.latest_at_or_before(
        month_end, datetime(9998, 12, 31, 10, tzinfo=UTC)
    ) == datetime(9998, 12, 31, 9, tzinfo=UTC)

    annual = yearly_type(
        local_time=time(9),
        dates=(
            month_day_type(month=1, day=1),
            month_day_type(month=2, day=29),
            month_day_type(month=12, day=31),
        ),
        timezone="UTC",
    )
    assert (
        recurrence_domain.RecurrenceCalculator.count_between(
            annual,
            datetime(2000, 1, 1, 9, tzinfo=UTC),
            datetime(2400, 12, 31, 9, tzinfo=UTC),
        )
        == 900
    )
    assert calls <= 12

    direct_calculation_calls = calls
    oldest = datetime(2000, 1, 31, 9, tzinfo=UTC)
    schedule_id = UUID("00000000-0000-0000-0000-000000002020")
    principal = _principal()
    async with build(
        settings=memory_settings(),
        storage="memory",
        fixed_clock_at=NOW,
        sequential_ids=True,
        principal=principal,
    ) as composition:
        schedule = Schedule(
            id=schedule_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            state=ScheduleState.ACTIVE,
            current_revision=1,
            next_fire_at=oldest,
            created_at=oldest,
            updated_at=oldest,
        )
        revision = ScheduleRevision(
            schedule_id=schedule_id,
            revision=1,
            title="Month-end archive",
            instruction="Archive the completed month.",
            agent_id=AGENT_ID,
            agent_version="1.0.0",
            policy_profile="default",
            requested_scopes=frozenset(),
            limits=RunLimits(
                max_steps=4,
                max_model_calls=4,
                max_tool_calls=4,
                max_cost=Decimal("1"),
            ),
            run_timeout_seconds=60,
            cadence=month_end,
            timezone="UTC",
            misfire_grace_seconds=60,
            max_consecutive_failures=3,
            created_by_principal_id=principal.principal_id,
            created_at=oldest,
        )
        async with composition.uow_factory() as uow:
            await uow.schedules.create(schedule, revision)
        materializer = ScheduleMaterializer(
            uow_factory=composition.uow_factory,
            principals=StaticSchedulePrincipalDirectory(principal),
            admission=AllowScheduleAdmissionController(),
            clock=composition.clock,
            ids=composition.ids,
            seed_checkpoint=DurableCheckpointSeeder(composition.clock),
        )

        occurrence = await materializer.materialize(schedule_id)
        async with composition.uow_factory() as uow:
            coalesced = await uow.process_events.list("schedule.misfires_coalesced")
            occurrences = await uow.schedule_occurrences.list(schedule_id, principal, limit=10)

    assert occurrence is not None
    assert occurrence.nominal_fire_at == datetime(2026, 7, 31, 9, tzinfo=UTC)
    assert occurrence.disposition is OccurrenceDisposition.MISSED
    assert len(occurrences) == 1
    assert len(coalesced) == 1
    assert coalesced[0].payload["count"] == 319
    assert calls - direct_calculation_calls <= 12


async def test_schedule_http_round_trips_every_calendar_kind() -> None:
    settings = replace(memory_settings(), schedule_api_enabled=True)
    expected_next = {
        "DAILY": datetime(2026, 8, 27, 20, tzinfo=UTC),
        "WEEKLY": datetime(2026, 8, 28, 10, tzinfo=UTC),
        "MONTHLY": datetime(2026, 8, 31, 9, tzinfo=UTC),
        "YEARLY": datetime(2026, 8, 28, 8, tzinfo=UTC),
    }
    async with build(
        settings=settings,
        storage="memory",
        fixed_clock_at=NOW,
        sequential_ids=True,
        principal=_principal(),
    ) as composition:
        async with composition.uow_factory() as uow:
            await uow.agents.put(_agent())
        async with _client(composition) as client:
            for index, cadence in enumerate(CALENDAR_CADENCES):
                created = await client.post(
                    "/v1/schedules",
                    json=_definition(cadence),
                    headers={"Idempotency-Key": f"calendar-{index}"},
                )
                assert created.status_code == 201, created.text
                body = created.json()
                schedule_id = body["schedule"]["id"]
                assert body["revision"]["cadence"]["kind"] == cadence["kind"]
                assert (
                    datetime.fromisoformat(body["schedule"]["next_fire_at"].replace("Z", "+00:00"))
                    == expected_next[str(cadence["kind"])]
                )

                updated_definition = _definition(cadence)
                updated_definition["title"] = f"Updated {cadence['kind']} task"
                updated = await client.patch(
                    f"/v1/schedules/{schedule_id}",
                    json={"expected_revision": 1, "definition": updated_definition},
                )
                assert updated.status_code == 200, updated.text
                assert updated.json()["revision"]["cadence"]["kind"] == cadence["kind"]
                assert (
                    datetime.fromisoformat(
                        updated.json()["schedule"]["next_fire_at"].replace("Z", "+00:00")
                    )
                    == expected_next[str(cadence["kind"])]
                )


@pytest.mark.parametrize("cadence", CALENDAR_CADENCES, ids=lambda value: str(value["kind"]))
async def test_schedule_create_supports_every_recurring_kind_through_approval(
    cadence: dict[str, object],
) -> None:
    arguments = _tool_arguments(cadence)
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="schedule.create",
                        arguments=arguments,
                        call_id=f"create-{cadence['kind']}",
                    )
                ]
            ),
            ScriptedTurn(text="I scheduled it."),
        ]
    )
    async with build(
        settings=replace(
            memory_settings(),
            schedule_api_enabled=True,
            schedule_worker_enabled=True,
        ),
        script=script,
        fixed_clock_at=NOW,
        sequential_ids=True,
        enabled_tools=["schedule.create"],
    ) as composition:
        run_id = await composition.runs.submit(f"Create my {cadence['kind']} schedule.")
        waiting = await composition.runs.get(run_id)
        [approval] = await composition.approvals.list_pending(run_id=run_id)
        assert waiting.status is RunStatus.WAITING_FOR_APPROVAL
        assert approval.arguments["title"] == arguments["title"]
        assert approval.arguments["instruction"] == arguments["instruction"]
        assert approval.arguments["requested_scopes"] == []
        assert cast(dict[str, object], approval.arguments["cadence"])["kind"] == cadence["kind"]

        await composition.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        completed = await composition.runs.wait_terminal(run_id)
        page = await composition.schedules.list(composition.principal, 10, None)

    assert completed.status is RunStatus.COMPLETED
    assert len(page.items) == 1
    assert page.items[0].revision.cadence.kind.value == cadence["kind"]
    assert page.items[0].revision.requested_scopes == frozenset()


async def test_schedule_create_calendar_validation_and_replay_fail_closed() -> None:
    valid = _tool_arguments(CALENDAR_CADENCES[2])
    async with build(
        settings=replace(
            memory_settings(),
            schedule_api_enabled=True,
            schedule_worker_enabled=True,
        ),
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
            idempotency_key="calendar-replay",
        )

        neither = await tool.execute(
            {"title": "Missing cadence", "instruction": "Do the task."}, context
        )
        both = await tool.execute({**valid, "at": "2026-08-28T10:00:00+00:00"}, context)
        impossible = await tool.execute(
            {
                "title": "Impossible annual date",
                "instruction": "Do the task.",
                "cadence": {
                    "kind": "YEARLY",
                    "local_time": "09:00:00",
                    "dates": [{"month": 4, "day": 31}],
                    "timezone": "UTC",
                },
            },
            context,
        )
        first = await tool.execute(valid, context)
        replay = await tool.execute(valid, context)
        mismatch = await tool.execute({**valid, "cadence": CALENDAR_CADENCES[3]}, context)
        page = await composition.schedules.list(composition.principal, 10, None)

    for result in (neither, both, impossible):
        assert result.ok is False
        assert result.failure is not None
        assert result.failure.kind is ToolFailureKind.INVALID_ARGUMENTS
    assert first.ok is True and replay.ok is True
    assert first.structured is not None and replay.structured is not None
    assert first.structured["schedule_id"] == replay.structured["schedule_id"]
    assert replay.structured["replayed"] is True
    assert mismatch.ok is False
    assert mismatch.failure is not None
    assert mismatch.failure.reason_code == "schedule.idempotency_mismatch"
    assert len(page.items) == 1
