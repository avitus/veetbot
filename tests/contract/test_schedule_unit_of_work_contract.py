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
    "notification_outbox",
    "queue",
}
FORBIDDEN_REPOSITORIES = {
    "approvals",
    "browser_authentications",
    "browser_grants",
    "browser_profiles",
    "evaluations",
    "invocations",
    "knowledge",
    "mcp_servers",
    "memories",
    "skills",
    "usage",
}


async def assert_schedule_unit_of_work_contract(factory: ScheduleUnitOfWorkFactory) -> None:
    async with factory() as uow:
        missing = {name for name in SCHEDULER_REPOSITORIES if not hasattr(uow, name)}
        assert not missing, f"scheduler unit of work is missing {sorted(missing)}"
        exposed = {name for name in FORBIDDEN_REPOSITORIES if hasattr(uow, name)}
        assert not exposed, f"scheduler unit of work exposes {sorted(exposed)}"
        assert callable(uow.on_rollback)


async def test_memory_unit_of_work_satisfies_schedule_contract() -> None:
    async with build(settings=memory_settings(), storage="memory") as composition:

        class RestrictedMemoryFactory:
            def __call__(self):  # type: ignore[no-untyped-def]
                uow = composition.uow_factory()
                for name in FORBIDDEN_REPOSITORIES:
                    if hasattr(uow, name):
                        delattr(uow, name)
                return uow

            def is_open(self) -> bool:
                return composition.uow_factory.is_open()

        await assert_schedule_unit_of_work_contract(
            cast(ScheduleUnitOfWorkFactory, RestrictedMemoryFactory())
        )
