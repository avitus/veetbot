"""Shared contract for the scheduler's least-privilege unit of work."""

from typing import cast

from agent_core.bootstrap import build
from agent_core.ports.persistence import ScheduleUnitOfWorkFactory
from tests.integration.m2_support import memory_settings

SCHEDULER_REPOSITORIES = {
    "agents",
    "process_events",
    "sessions",
    "runs",
    "events",
    "history",
    "checkpoints",
    "schedules",
    "schedule_occurrences",
    "schedule_admission",
    "queue",
}


async def assert_schedule_unit_of_work_contract(factory: ScheduleUnitOfWorkFactory) -> None:
    async with factory() as uow:
        assert all(hasattr(uow, name) for name in SCHEDULER_REPOSITORIES)
        assert callable(uow.on_rollback)


async def test_memory_unit_of_work_satisfies_schedule_contract() -> None:
    async with build(settings=memory_settings(), storage="memory") as composition:
        await assert_schedule_unit_of_work_contract(
            cast(ScheduleUnitOfWorkFactory, composition.uow_factory)
        )
