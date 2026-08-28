"""PostgreSQL round-trip coverage for Milestone 20 calendar cadences."""

from datetime import UTC, datetime, time
from decimal import Decimal

from agent_core.bootstrap import build
from agent_core.domain.runs import RunLimits
from agent_core.domain.schedules import (
    Cadence,
    MonthDay,
    MonthlyCadence,
    ScheduleDefinition,
    YearlyCadence,
)
from tests.contract.support import agent
from tests.integration.m2_support import database_settings

NOW = datetime(2026, 8, 27, 19, tzinfo=UTC)


async def test_postgres_round_trips_monthly_and_yearly_revision_definitions() -> None:
    cadences: tuple[Cadence, ...] = (
        MonthlyCadence(
            local_time=time(9),
            days_of_month=(1, 31),
            last_day=True,
            timezone="America/Los_Angeles",
        ),
        YearlyCadence(
            local_time=time(8),
            dates=(MonthDay(month=2, day=29), MonthDay(month=12, day=25)),
            timezone="Europe/London",
        ),
    )
    async with build(
        settings=database_settings(),
        storage="postgres",
        fixed_clock_at=NOW,
        sequential_ids=True,
    ) as composition:
        pinned = agent()
        async with composition.uow_factory() as uow:
            await uow.agents.put(pinned)

        schedule_ids = []
        for index, cadence in enumerate(cadences):
            record = await composition.schedules.create(
                composition.principal,
                ScheduleDefinition(
                    title=f"Calendar recurrence {index}",
                    instruction="Run the calendar recurrence.",
                    agent_id=pinned.id,
                    agent_version=pinned.version,
                    policy_profile=pinned.policy_profile,
                    requested_scopes=frozenset(),
                    limits=RunLimits(
                        max_steps=4,
                        max_model_calls=4,
                        max_tool_calls=4,
                        max_cost=Decimal("1"),
                    ),
                    run_timeout_seconds=60,
                    cadence=cadence,
                    misfire_grace_seconds=60,
                    max_consecutive_failures=3,
                ),
                f"calendar-postgres-{index}",
            )
            schedule_ids.append(record.schedule.id)

        reloaded = [
            await composition.schedules.get(composition.principal, schedule_id)
            for schedule_id in schedule_ids
        ]

    assert tuple(record.revision.cadence for record in reloaded) == cadences
    assert all(record.schedule.next_fire_at is not None for record in reloaded)
