"""Shared lifecycle contract for scheduler unit-of-work factories."""

from typing import cast

from agent_core.bootstrap import build
from agent_core.ports.persistence import ScheduleUnitOfWorkFactory
from tests.integration.m2_support import memory_settings


async def assert_schedule_unit_of_work_factory_contract(
    factory: ScheduleUnitOfWorkFactory,
) -> None:
    assert factory.is_open() is False
    async with factory():
        assert factory.is_open() is True
    assert factory.is_open() is False


async def test_memory_unit_of_work_factory_satisfies_schedule_contract() -> None:
    async with build(settings=memory_settings(), storage="memory") as composition:
        await assert_schedule_unit_of_work_factory_contract(
            cast(ScheduleUnitOfWorkFactory, composition.uow_factory)
        )
