"""Behavioral contract for the people import formation composition interface."""

from agent_core.memory.formation import GovernedMemoryService
from tests.contract.memory_fixtures import user_event
from tests.contract.support import SESSION_ID, ids, memory_uow_factory, principal


async def test_import_window_uses_only_original_selected_events_and_keeps_automatic_cursor() -> (
    None
):
    from agent_core.memory.formation import HighRecallCandidateExtractor

    clock, factory = await memory_uow_factory()
    first = await user_event(factory, "I prefer jasmine tea.")
    await user_event(factory, "I prefer coffee in the morning.")
    async with factory() as uow:
        events = await uow.events.list_after(SESSION_ID, first - 1, principal(), limit=1)
    from agent_core.ports.people_runtime import PeopleImportFormation

    service: PeopleImportFormation = GovernedMemoryService(
        factory,
        clock,
        ids(),
        principal(),
        extractor=HighRecallCandidateExtractor(),
        policy_version="formation@11",
        people_enabled=True,
    )
    result = await service.run(
        trigger="people_import", scope="user", session_id=SESSION_ID, source_window=tuple(events)
    )
    assert result.beliefs and all("coffee" not in record.statement for record in result.beliefs)
    async with factory() as uow:
        assert await uow.memories.consolidation_watermark(SESSION_ID, principal()) == 0
