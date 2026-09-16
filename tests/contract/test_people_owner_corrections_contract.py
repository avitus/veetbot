"""Behavioral contract for the people owner corrections composition interface."""

from tests.contract.support import principal


async def test_owner_correction_preserves_old_fact_and_uses_new_source() -> None:
    from agent_core.domain.memory import MemoryStatus
    from tests.contract.memory_fixtures import formation_stack, user_event
    from tests.contract.support import SESSION_ID

    _clock, factory, memory_service, _retriever = await formation_stack()
    source = await user_event(factory, "Alex lives in Paris")
    belief = await memory_service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="Alex lives in Paris",
        subject="Alex residence",
        scope="user",
        source_event_ids=[source],
    )
    from agent_core.ports.people_runtime import PeopleOwnerCorrections

    contract: PeopleOwnerCorrections = memory_service
    correction = await user_event(factory, "Alex moved to Berlin")
    async with factory() as uow:
        replacement = await contract.correct_from_owner(
            belief.id,
            session_id=SESSION_ID,
            source_event_id=correction,
            operation="changed",
            statement="Alex lives in Berlin",
            expected_position=belief.store_position,
            existing_uow=uow,
        )
        assert replacement is not None and replacement.source_event_ids == [correction]
        old = await uow.memories.get(belief.id, principal())
        assert old.statement == "Alex lives in Paris" and old.status is MemoryStatus.SUPERSEDED
        assert old.superseded_by == replacement.id
