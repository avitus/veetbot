"""M33 semantic-index contracts on migrated PostgreSQL and real transactions."""

from collections.abc import Awaitable, Callable

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import build
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.semantic_memory_cases import SCENARIOS
from tests.contract.support import NOW, principal, session
from tests.integration.m2_support import database_settings


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda case: case.__name__)
async def test_semantic_index(scenario: Callable[[UnitOfWorkFactory], Awaitable[None]]) -> None:
    async with build(
        settings=database_settings(),
        storage="postgres",
        principal=principal(),
        clock=FixedClock(NOW),
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        await scenario(app.uow_factory)


async def test_inflight_vector_cannot_commit_across_correction() -> None:
    import asyncio

    from tests.contract.memory_fixtures import memory, recall_query
    from tests.contract.semantic_memory_cases import SPACE, VECTOR

    async with build(
        settings=database_settings(),
        storage="postgres",
        principal=principal(),
        clock=FixedClock(NOW),
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
            original = await uow.memories.upsert_belief(memory())
            source = await uow.semantic_memory.capture(principal(), original.id, SPACE)
            assert source is not None
        changed, release = asyncio.Event(), asyncio.Event()

        async def correct() -> None:
            async with app.uow_factory() as uow:
                await uow.memories.reinforce(
                    original.model_copy(update={"statement": "User now prefers detailed answers"})
                )
                changed.set()
                await release.wait()

        async def complete() -> bool:
            async with app.uow_factory() as uow:
                return await uow.semantic_memory.put(principal(), source, VECTOR)

        async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
            tasks.create_task(correct())
            await changed.wait()
            pending = tasks.create_task(complete())
            await asyncio.sleep(0.05)
            assert not pending.done(), "source commit and vector CAS must serialize"
            release.set()
        assert not pending.result()
        async with app.uow_factory() as uow:
            assert await uow.semantic_memory.search(recall_query(), SPACE, VECTOR, limit=1) == []


async def test_vector_rows_are_owner_bound_erased_and_tenant_isolated() -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from agent_core.adapters.persistence.semantic_memory import PostgresSemanticMemoryIndex
    from tests.contract.memory_fixtures import memory
    from tests.contract.semantic_memory_cases import SPACE, VECTOR
    from tests.contract.support import TENANT

    async with build(
        settings=database_settings(),
        storage="postgres",
        principal=principal(),
        clock=FixedClock(NOW),
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
            original = await uow.memories.upsert_belief(memory())
            source = await uow.semantic_memory.capture(principal(), original.id, SPACE)
            assert source is not None and await uow.semantic_memory.put(principal(), source, VECTOR)
        async with app.uow_factory() as uow:
            assert isinstance(uow.semantic_memory, PostgresSemanticMemoryIndex)
            connection = uow.semantic_memory._session
            with pytest.raises(IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(
                        text("UPDATE memory_vectors SET principal_id = 'foreign'")
                    )
            async with connection.begin_nested() as nested:
                try:
                    await connection.execute(
                        text("CREATE ROLE m33_vector_reader NOSUPERUSER NOBYPASSRLS")
                    )
                    await connection.execute(
                        text("GRANT USAGE ON SCHEMA public TO m33_vector_reader")
                    )
                    await connection.execute(
                        text("GRANT SELECT ON memory_vectors TO m33_vector_reader")
                    )
                    await connection.execute(text("SET LOCAL ROLE m33_vector_reader"))
                    for tenant, count in ((TENANT, 1), ("foreign", 0)):
                        await connection.execute(
                            text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                            {"tenant": tenant},
                        )
                        assert (
                            await connection.scalar(text("SELECT count(*) FROM memory_vectors"))
                            == count
                        )
                finally:
                    await nested.rollback()
            await uow.memories.fence_for_erasure(principal(), [original.id])
            assert await connection.scalar(text("SELECT count(*) FROM memory_vectors")) == 0
