"""Persona nomination out of governed consolidation (Milestone 22)."""

from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.domain.memory import ConsolidationResult
from agent_core.domain.persona import PersonaNominationState
from agent_core.memory.formation import GovernedMemoryService
from tests.contract.memory_fixtures import formation_stack, user_event
from tests.contract.support import SESSION_ID, principal


async def _consolidate_preference_three_times(
    factory: MemoryUnitOfWorkFactory, service: GovernedMemoryService
) -> ConsolidationResult:
    """Reinforce one user-scope preference across three consolidations."""

    result: ConsolidationResult | None = None
    for _ in range(3):
        await user_event(factory, "I prefer concise answers")
        result = await service.run(trigger="session_close", scope="user", session_id=SESSION_ID)
    assert result is not None
    return result


async def test_consolidation_nominates_a_qualifying_belief_exactly_once() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    result = await _consolidate_preference_three_times(factory, service)

    belief = next(
        record for record in await service.list_memories() if record.corroboration_count >= 3
    )
    assert belief.confidence >= 0.75
    assert belief.scope == "user"

    async with factory() as uow:
        open_rows = await uow.personas.list_nominations(
            principal(), state=PersonaNominationState.NOMINATED
        )
    assert [row.belief_id for row in open_rows] == [belief.id]
    nomination = open_rows[0]
    assert nomination.statement == belief.statement
    assert nomination.consolidation_run_id == result.run.id

    # A further consolidation of the same evidence does not duplicate it.
    await user_event(factory, "I prefer concise answers")
    await service.run(trigger="session_close", scope="user", session_id=SESSION_ID)
    async with factory() as uow:
        replayed = await uow.personas.list_nominations(
            principal(), state=PersonaNominationState.NOMINATED
        )
    assert [row.id for row in replayed] == [nomination.id]


async def test_a_declined_belief_is_never_renominated() -> None:
    clock, factory, service, _retriever = await formation_stack()
    await _consolidate_preference_three_times(factory, service)

    async with factory() as uow:
        nomination = (
            await uow.personas.list_nominations(principal(), state=PersonaNominationState.NOMINATED)
        )[0]
        await uow.personas.resolve_nomination(
            nomination.id,
            principal(),
            state=PersonaNominationState.DECLINED,
            resolved_at=clock.now(),
        )

    await user_event(factory, "I prefer concise answers")
    await service.run(trigger="session_close", scope="user", session_id=SESSION_ID)

    async with factory() as uow:
        open_rows = await uow.personas.list_nominations(
            principal(), state=PersonaNominationState.NOMINATED
        )
    assert open_rows == []


async def test_project_scope_beliefs_are_not_nominated() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    for _ in range(3):
        await user_event(factory, "I prefer concise answers")
        await service.run(trigger="session_close", scope="project-a", session_id=SESSION_ID)

    async with factory() as uow:
        open_rows = await uow.personas.list_nominations(principal())
    assert open_rows == []
