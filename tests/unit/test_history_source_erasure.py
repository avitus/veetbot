"""Source erasure preserves complete, unrelated historical revisions."""

from uuid import uuid4

from tests.contract.memory_fixtures import memory
from tests.contract.semantic_memory_cases import SPACE
from tests.contract.support import NOW, memory_uow_factory, principal, session


async def test_session_deletion_preserves_unrelated_historical_source_version() -> None:
    _, factory = await memory_uow_factory()
    other = session().model_copy(update={"id": uuid4()})
    async with factory() as uow:
        await uow.sessions.create(other)
        retained = await uow.memories.upsert_belief(
            memory(statement="Keep unrelated history").model_copy(
                update={"source_session_id": other.id}
            )
        )
        captured = await uow.semantic_memory.capture(principal(), retained.id, SPACE)
        assert captured is not None
    async with factory() as uow:
        await uow.session_deletions.delete(session().id, principal(), NOW)
    async with factory() as uow:
        assert await uow.memories.get_at(retained.id, principal(), known_at=NOW) == retained
        historical = await uow.semantic_memory.capture_revision(principal(), captured.source, SPACE)
        assert historical is not None and historical.record == retained
