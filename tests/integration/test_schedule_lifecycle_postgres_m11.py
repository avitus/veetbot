"""PostgreSQL lifecycle linearization for Milestone 11 schedules."""

import asyncio
from datetime import UTC, datetime, time
from decimal import Decimal

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.identity import StaticSchedulePrincipalDirectory
from agent_core.adapters.schedule_admission import AllowScheduleAdmissionController
from agent_core.bootstrap import build
from agent_core.domain.errors import ConflictError
from agent_core.domain.runs import RunLimits, RunStatus
from agent_core.domain.schedules import DailyCadence, ScheduleDefinition
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from agent_core.scheduling.accounting import ScheduleOutcomeAccountant
from agent_core.scheduling.materializer import ScheduleMaterializer
from tests.contract.support import agent
from tests.integration.m2_support import database_settings

NOW = datetime(2026, 8, 20, 16, tzinfo=UTC)


def _definition(instruction: str) -> ScheduleDefinition:
    pinned = agent()
    return ScheduleDefinition(
        title="PostgreSQL lifecycle",
        instruction=instruction,
        agent_id=pinned.id,
        agent_version=pinned.version,
        policy_profile=pinned.policy_profile,
        requested_scopes=frozenset({"workspace.read"}),
        limits=RunLimits(
            max_steps=4,
            max_model_calls=4,
            max_tool_calls=4,
            max_cost=Decimal("1"),
        ),
        run_timeout_seconds=60,
        cadence=DailyCadence(local_time=time(9), timezone="UTC"),
        misfire_grace_seconds=60,
        max_consecutive_failures=3,
    )


async def test_postgres_lifecycle_linearizes_idempotency_and_revision_races() -> None:
    async with build(
        settings=database_settings(), storage="postgres", fixed_clock_at=NOW
    ) as composition:
        async with composition.uow_factory() as uow:
            await uow.agents.put(agent())

        first, second = await asyncio.gather(
            composition.schedules.create(
                composition.principal, _definition("same request"), "same-key"
            ),
            composition.schedules.create(
                composition.principal, _definition("same request"), "same-key"
            ),
        )
        assert first.schedule.id == second.schedule.id

        results = await asyncio.gather(
            composition.schedules.update(
                composition.principal,
                first.schedule.id,
                1,
                _definition("winner one"),
            ),
            composition.schedules.update(
                composition.principal,
                first.schedule.id,
                1,
                _definition("winner two"),
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(result, BaseException) for result in results) == 1
        [loser] = [result for result in results if isinstance(result, BaseException)]
        assert isinstance(loser, ConflictError)

        current = await composition.schedules.get(composition.principal, first.schedule.id)
        assert current.schedule.current_revision == 2


async def test_postgres_terminal_accounting_is_exactly_once_under_concurrency() -> None:
    async with build(
        settings=database_settings(), storage="postgres", fixed_clock_at=NOW
    ) as composition:
        async with composition.uow_factory() as uow:
            await uow.agents.put(agent())
        record = await composition.schedules.create(
            composition.principal,
            _definition("fail once"),
            "accounting-concurrency",
        )
        clock = composition.clock
        assert isinstance(clock, FixedClock)
        assert record.schedule.next_fire_at is not None
        clock.advance(record.schedule.next_fire_at - clock.now())
        occurrence = await ScheduleMaterializer(
            uow_factory=composition.uow_factory,
            principals=StaticSchedulePrincipalDirectory(composition.principal),
            admission=AllowScheduleAdmissionController(),
            clock=clock,
            ids=composition.ids,
            seed_checkpoint=DurableCheckpointSeeder(clock),
        ).materialize(record.schedule.id)
        assert occurrence is not None and occurrence.run_id is not None
        async with composition.uow_factory() as uow:
            await uow.runs.transition(occurrence.run_id, RunStatus.QUEUED, RunStatus.RUNNING)
            await uow.runs.transition(occurrence.run_id, RunStatus.RUNNING, RunStatus.FAILED)
        accountant = ScheduleOutcomeAccountant(
            uow_factory=composition.uow_factory,
            clock=clock,
            ids=composition.ids,
        )
        results = await asyncio.gather(
            accountant.account(occurrence.run_id),
            accountant.account(occurrence.run_id),
        )
        assert sorted(results) == [False, True]
        current = await composition.schedules.get(composition.principal, record.schedule.id)
        assert current.schedule.consecutive_failures == 1
