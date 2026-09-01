"""Persona nomination out of governed consolidation (Milestone 22)."""

from uuid import UUID

from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.domain.memory import ConsolidationResult, MemoryStatus
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


async def test_a_declined_statement_survives_rederivation_with_a_new_belief_id() -> None:
    """Decline is content-keyed: re-derivation mints new belief ids, and the
    same statement must not come back under one."""

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

    # Re-derivation's shape: the old belief is superseded by a fresh record
    # carrying the same statement under a new identifier.
    async with factory() as uow:
        old = await uow.memories.get(nomination.belief_id, principal())
        replacement = old.model_copy(
            update={
                "id": UUID("00000000-0000-0000-0000-000000000777"),
                "store_position": await uow.memories.next_position(),
                "superseded_by": None,
            }
        )
        superseded = old.model_copy(
            update={
                "status": MemoryStatus.SUPERSEDED,
                "valid_to": clock.now(),
                "superseded_by": replacement.id,
                "store_position": await uow.memories.next_position(),
            }
        )
        await uow.memories.supersede(superseded, replacement)

    # The next consolidation reinforces the replacement, re-qualifying it.
    await user_event(factory, "I prefer concise answers")
    await service.run(trigger="session_close", scope="user", session_id=SESSION_ID)

    async with factory() as uow:
        open_rows = await uow.personas.list_nominations(
            principal(), state=PersonaNominationState.NOMINATED
        )
    assert open_rows == []


async def test_stale_nominations_withdraw_even_on_a_pass_with_no_new_beliefs() -> None:
    """Reconciliation runs on every non-retry consolidation, not only on one
    that happens to commit something."""

    _clock, factory, service, _retriever = await formation_stack()
    await _consolidate_preference_three_times(factory, service)
    async with factory() as uow:
        nomination = (
            await uow.personas.list_nominations(principal(), state=PersonaNominationState.NOMINATED)
        )[0]
    await service.delete(nomination.belief_id)

    # A pass over an event that forms nothing must still reconcile.
    await user_event(factory, "Nothing memorable here at all")
    await service.run(trigger="session_close", scope="user", session_id=SESSION_ID)

    async with factory() as uow:
        withdrawn = await uow.personas.get_nomination(nomination.id, principal())
    assert withdrawn.state is PersonaNominationState.WITHDRAWN
