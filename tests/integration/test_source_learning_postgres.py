"""Source learning persists attribution and erasure across PostgreSQL transactions."""

from tests.unit.test_source_learning import (
    assert_cross_chat_erasure,
    assert_forgetting_erases_receipt,
    learn,
    observation,
    read_archive,
)


async def test_postgres_source_learning_is_persisted_and_idempotent() -> None:
    async with read_archive(storage="postgres") as (app, context, _):
        first = await learn(app, context, observation())
        assert first[0]["status"] == "committed"
        before = await app.memory.list_memories()
        again = await learn(app, context, observation())
        assert again == [{"status": "unchanged", "belief_id": str(before[0].id)}]
        assert await app.memory.list_memories() == before


async def test_postgres_supporting_chat_erasure_fences_cross_chat_memory() -> None:
    await assert_cross_chat_erasure("postgres")


async def test_source_learning_downgrade_refuses_to_erase_attribution() -> None:
    import subprocess

    import pytest

    from tests.integration.test_migrations import _alembic

    async with read_archive(storage="postgres") as (app, context, _):
        assert (await learn(app, context, observation()))[0]["status"] == "committed"
    with pytest.raises(subprocess.CalledProcessError) as failure:
        _alembic("downgrade", "d5f2b8c3a216")
    assert "Source-learning attribution must be preserved" in failure.value.stderr
    assert "e6a3c9d4b217" in _alembic("current")


async def test_postgres_forgetting_erases_source_learning_receipts() -> None:
    await assert_forgetting_erases_receipt("postgres")
