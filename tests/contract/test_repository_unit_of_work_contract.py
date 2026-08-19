from agent_core.adapters.persistence.memory import (
    InMemoryAgentRepository,
    InMemoryToolInvocationRepository,
)
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.bootstrap import _memory_uow_repositories
from tests.contract.support import NOW, agent, memory_stack


async def test_repository_unit_of_work_exposes_one_repository_set() -> None:
    clock, sessions, runs, events = await memory_stack()
    agents = InMemoryAgentRepository()
    invocations = InMemoryToolInvocationRepository(runs)
    factory = MemoryUnitOfWorkFactory(
        _memory_uow_repositories(
            agents=agents,
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=invocations,
            clock=clock,
        )
    )
    async with factory() as uow:
        assert uow.browser_profiles is not None
        configured = agent()
        await uow.agents.put(configured)
        assert await uow.agents.latest_version(configured.id) == configured
    assert clock.now() == NOW
