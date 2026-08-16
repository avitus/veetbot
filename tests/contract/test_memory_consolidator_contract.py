"""Memory consolidation contract: extraction, watermarks, and audit rows."""

import inspect

from agent_core.domain.memory import MemoryStatus
from agent_core.memory.formation import GovernedMemoryService
from tests.contract.memory_fixtures import formation_stack, user_event
from tests.contract.support import SESSION_ID, principal


def test_governed_memory_service_exposes_async_consolidation() -> None:
    assert inspect.iscoroutinefunction(GovernedMemoryService.run)


async def test_consolidation_extracts_candidates_and_advances_the_watermark() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    await user_event(factory, "What time is it?")
    await user_event(factory, "Remember that my launch code is ORBIT-9")
    last = await user_event(factory, "We really prefer tabs over spaces")

    result = await service.run(trigger="session_close", scope="project-a", session_id=SESSION_ID)

    assert sorted(belief.statement for belief in result.beliefs) == [
        "Prefers tabs over spaces",
        "my launch code is ORBIT-9",
    ]
    assert {belief.status for belief in result.beliefs} == {MemoryStatus.PROVISIONAL}
    audit = result.run
    assert (audit.candidates_proposed, audit.committed, audit.rejected) == (2, 2, 0)
    assert audit.watermark_before == 0
    assert audit.watermark_after == last
    async with factory() as uow:
        assert await uow.memories.consolidation_watermark(SESSION_ID, principal()) == last


async def test_consolidation_is_incremental_after_the_watermark() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    await user_event(factory, "Remember that my launch code is ORBIT-9")
    first = await service.run(trigger="session_close", scope="project-a", session_id=SESSION_ID)
    assert len(first.beliefs) == 1

    second = await service.run(trigger="session_close", scope="project-a", session_id=SESSION_ID)
    assert second.beliefs == []
    assert second.run.candidates_proposed == 0
    assert [belief.statement for belief in await service.list_memories()] == [
        "my launch code is ORBIT-9"
    ]


async def test_consolidation_without_a_session_still_records_an_audit_row() -> None:
    _clock, _factory, service, _retriever = await formation_stack()
    result = await service.run(trigger="scheduled", scope="general", session_id=None)
    assert result.beliefs == []
    assert result.run.trigger == "scheduled"
    assert result.run.finished_at is not None
