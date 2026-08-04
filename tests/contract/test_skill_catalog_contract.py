"""Session-pinned skill catalog contract."""

from agent_core.adapters.persistence.memory import (
    InMemoryAgentRepository,
    InMemoryToolInvocationRepository,
)
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.adapters.skills.stores import InMemorySkillPackageStore
from agent_core.bootstrap import _memory_uow_repositories
from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.skills.catalog import SkillCatalogService
from tests.contract.support import agent, memory_stack, principal, session


async def test_skill_catalog_is_stable_for_an_open_session() -> None:
    clock, sessions, runs, events = await memory_stack()
    factory = MemoryUnitOfWorkFactory(
        _memory_uow_repositories(
            agents=InMemoryAgentRepository(),
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=InMemoryToolInvocationRepository(runs),
            clock=clock,
        )
    )
    catalogs = SkillCatalogService(
        factory,
        InMemorySkillPackageStore(),
        ConservativeTokenEstimator(),
    )
    first = await catalogs.open(session().id, agent(), principal())
    second = await catalogs.open(session().id, agent(), principal())
    assert first == second
    assert first.entries == ()
