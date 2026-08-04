"""Session-pinned skill catalog contract."""

from uuid import UUID

from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.adapters.persistence.memory import (
    InMemoryAgentRepository,
    InMemoryToolInvocationRepository,
)
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.adapters.skills.memory import InMemorySkillRepository
from agent_core.adapters.skills.stores import InMemorySkillPackageStore
from agent_core.bootstrap import _memory_uow_repositories
from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.domain.skills import SkillPackage, SkillPackageMember, SkillSource
from agent_core.skills.catalog import SkillCatalogService
from agent_core.skills.package import SkillPackageValidator
from tests.contract.support import agent, memory_stack, principal, session


async def test_skill_catalog_is_stable_for_an_open_session() -> None:
    clock, sessions, runs, events = await memory_stack()
    store = InMemorySkillPackageStore()
    repository = InMemorySkillRepository(
        store,
        SkillPackageValidator(ConservativeTokenEstimator()),
        clock,
        SequenceIdFactory(),
    )

    def package(name: str) -> SkillPackage:
        return SkillPackage(
            directory_name=name,
            members=(
                SkillPackageMember(
                    path="SKILL.md",
                    data=(
                        f"---\nname: {name}\nversion: 1.0.0\n"
                        "description: Contract skill.\nrequired_tools: []\n---\nbody"
                    ).encode(),
                ),
            ),
        )

    await repository.install("tenant-a", package("contract-one"), SkillSource.OPERATOR, None, None)
    factory = MemoryUnitOfWorkFactory(
        _memory_uow_repositories(
            agents=InMemoryAgentRepository(),
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=InMemoryToolInvocationRepository(runs),
            skills=repository,
            clock=clock,
        )
    )
    catalogs = SkillCatalogService(
        factory,
        store,
        ConservativeTokenEstimator(),
    )
    first_agent = agent().model_copy(update={"enabled_skills": ["contract-one"]})
    first = await catalogs.open(session().id, first_agent, principal())
    stable = await catalogs.open(session().id, first_agent, principal())
    assert first == stable
    assert [entry.manifest.name for entry in first.entries] == ["contract-one"]

    await repository.install("tenant-a", package("contract-two"), SkillSource.OPERATOR, None, None)
    second_session = session().model_copy(update={"id": UUID(int=21)})
    await sessions.create(second_session)
    second_agent = agent().model_copy(update={"enabled_skills": ["contract-one", "contract-two"]})
    second = await catalogs.open(second_session.id, second_agent, principal())
    assert [entry.manifest.name for entry in second.entries] == [
        "contract-one",
        "contract-two",
    ]
