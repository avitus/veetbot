"""Feed upkeep stays in the operational inbox, including pre-upgrade occurrences."""

from sqlalchemy import text

from agent_core.adapters.persistence.database import create_engine
from agent_core.bootstrap import build
from tests.contract.test_session_repository_contract import (
    assert_session_index_filters_before_pagination,
    assert_session_index_preserves_non_boolean_refresh_markers,
)
from tests.gates.test_cockpit_feed_refresh_adr0254 import (
    NOW,
    OWNER,
    _create,
    _id,
    _materializer,
    _rows,
)
from tests.integration.m2_support import database_settings
from tests.integration.test_migrations import _alembic


async def test_postgres_filters_refreshes_before_pagination() -> None:
    async with (
        build(settings=database_settings(), storage="postgres") as composition,
        composition.uow_factory() as uow,
    ):
        await assert_session_index_filters_before_pagination(uow.sessions)


async def test_postgres_preserves_non_boolean_refresh_markers() -> None:
    """Legacy metadata cannot be cast to boolean or break the conversation index."""
    async with (
        build(settings=database_settings(), storage="postgres") as composition,
        composition.uow_factory() as uow,
    ):
        await assert_session_index_preserves_non_boolean_refresh_markers(uow.sessions)


async def test_upgrade_marks_old_feed_occurrences_without_erasing_history() -> None:
    from tests.gates.test_cockpit_feed_refresh_adr0254 import _agent

    settings = database_settings()
    async with build(
        settings=settings, storage="postgres", principal=OWNER, fixed_clock_at=NOW
    ) as composition:
        async with composition.uow_factory() as uow:
            await uow.agents.put(_agent())
        feed, revision = _rows(_id(921))
        await _create(composition, (feed, revision))
        occurrence, _ = await _materializer(composition).materialize_manual(
            OWNER, feed.id, expected_revision=1, request_key="old-refresh"
        )
    engine = create_engine(settings.database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE sessions SET metadata = metadata - 'cockpit_refresh' WHERE id = :id"),
                {"id": occurrence.session_id},
            )
        _alembic("downgrade", "d8a268b10301")
        _alembic("upgrade", "head")
        async with engine.connect() as connection:
            metadata = (
                await connection.execute(
                    text("SELECT metadata FROM sessions WHERE id = :id"),
                    {"id": occurrence.session_id},
                )
            ).scalar_one()
            assert metadata.get("cockpit_refresh") is True
            assert (
                await connection.execute(
                    text("SELECT count(*) FROM events WHERE session_id = :id"),
                    {"id": occurrence.session_id},
                )
            ).scalar_one() > 0
    finally:
        _alembic("upgrade", "head")
        await engine.dispose()
