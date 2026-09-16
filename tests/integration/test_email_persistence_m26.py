"""PostgreSQL email records: contract parity, reservations and dispatch serialization."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agent_core.adapters.persistence.database import create_engine, create_session_factory
from agent_core.adapters.persistence.email import PostgresEmailStore
from agent_core.adapters.persistence.sqlalchemy_models import EmailRecordRow
from agent_core.domain.errors import ConflictError
from tests.contract.support import principal
from tests.contract.test_email_store_contract import (
    assert_email_store_contract,
    email_import_window_contract,
    record,
)
from tests.integration.m2_support import database_settings


@asynccontextmanager
async def database() -> AsyncIterator[AsyncEngine]:
    engine = create_engine(database_settings().database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


async def configure(session: AsyncSession) -> None:
    await session.execute(
        text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
        {"tenant": principal().tenant_id},
    )


async def test_postgres_email_store_satisfies_the_shared_contract() -> None:
    async with database() as engine, create_session_factory(engine)() as session:
        await configure(session)
        await assert_email_store_contract(PostgresEmailStore(session))
        await email_import_window_contract(PostgresEmailStore(session))
        await session.commit()


async def test_postgres_thread_summaries_omit_content_and_mask_pending_erasure() -> None:
    from tests.contract.test_email_store_contract import thread_record, thread_summary_contract

    async with database() as engine, create_session_factory(engine)() as session:
        await configure(session)
        store = PostgresEmailStore(session)
        await thread_summary_contract(store)
        await store.put(thread_record("summary-erased"), expected_revision=0)
        await session.execute(
            update(EmailRecordRow)
            .where(EmailRecordRow.kind == "thread", EmailRecordRow.key == "summary-erased")
            .values(erasure_pending=True)
        )
        [erased] = await store.list_thread_summaries(principal(), after="summary-c")
        full = await store.get(principal(), "thread", "summary-erased")
        assert full is not None
        assert erased.payload == {k: v for k, v in full.payload.items() if k != "messages"}
        assert erased.payload["reason"] == "Related People memory was erased."
        await session.rollback()


async def test_email_records_roll_back_with_the_owning_transaction() -> None:
    async with database() as engine:
        factory = create_session_factory(engine)
        async with factory() as session:
            await configure(session)
            await PostgresEmailStore(session).put(record(), expected_revision=0)
            await session.rollback()
        async with factory() as session:
            await configure(session)
            assert await PostgresEmailStore(session).get(principal(), "draft", "a") is None


async def test_concurrent_email_revision_claim_has_one_winner() -> None:
    async with database() as engine:
        factory = create_session_factory(engine)
        async with factory() as session:
            await configure(session)
            await PostgresEmailStore(session).put(record(), expected_revision=0)
            await session.commit()

        async def claim() -> bool:
            async with factory() as session:
                await configure(session)
                try:
                    await PostgresEmailStore(session).put(record(revision=2), expected_revision=1)
                except ConflictError:
                    await session.rollback()
                    return False
                await session.commit()
                return True

        assert sorted(await asyncio.gather(claim(), claim())) == [False, True]


async def test_email_principal_lock_lasts_through_transaction_commit() -> None:
    async with database() as engine:
        factory = create_session_factory(engine)
        waiting = asyncio.Event()
        acquired = asyncio.Event()
        async with factory() as first:
            await configure(first)
            async with PostgresEmailStore(first).lock(principal()):
                pass  # Leaving the lexical lock must not release the transaction lock.

            async def contender() -> None:
                async with factory() as second:
                    await configure(second)
                    waiting.set()
                    async with PostgresEmailStore(second).lock(principal()):
                        acquired.set()
                    await second.commit()

            task = asyncio.create_task(contender())
            try:
                await asyncio.wait_for(waiting.wait(), timeout=2)
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(acquired.wait(), timeout=0.1)
                async with factory() as independent:
                    await configure(independent)
                    foreign = principal().model_copy(update={"principal_id": "independent"})
                    async with asyncio.timeout(2):
                        async with PostgresEmailStore(independent).lock(foreign):
                            pass
                    await independent.commit()
                await first.commit()
                await asyncio.wait_for(task, timeout=2)
                assert acquired.is_set()
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


async def test_email_records_force_tenant_rls_for_non_superuser() -> None:
    async with database() as engine:
        factory = create_session_factory(engine)
        role = f"email26_probe_{uuid4().hex}"
        async with factory() as admin:
            await configure(admin)
            await PostgresEmailStore(admin).put(record(), expected_revision=0)
            await admin.execute(text(f"CREATE ROLE {role} NOLOGIN"))
            await admin.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            await admin.execute(text(f"GRANT SELECT, INSERT ON email_records TO {role}"))
            await admin.commit()
        try:
            async with factory() as restricted:
                await configure(restricted)
                await restricted.execute(text(f"SET LOCAL ROLE {role}"))
                visible = (await restricted.execute(select(EmailRecordRow))).scalars().all()
                assert len(visible) == 1
                await restricted.execute(
                    text("SELECT set_config('agent_core.tenant_id', 'foreign', true)")
                )
                assert (await restricted.execute(select(EmailRecordRow))).scalars().all() == []
                with pytest.raises(DBAPIError) as denied:
                    async with restricted.begin_nested():
                        await PostgresEmailStore(restricted).put(
                            record("forbidden"), expected_revision=0
                        )
                assert getattr(denied.value.orig, "sqlstate", None) == "42501"
                await restricted.rollback()
            async with factory() as admin:
                flags = (
                    await admin.execute(
                        text(
                            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                            "WHERE relname='email_records'"
                        )
                    )
                ).one()
                assert flags == (True, True)
        finally:
            async with factory() as admin:
                await admin.execute(text(f"DROP OWNED BY {role}"))
                await admin.execute(text(f"DROP ROLE {role}"))
                await admin.commit()


async def test_postgres_email_session_and_event_query_filters() -> None:
    from agent_core.bootstrap import build
    from tests.contract.support import session as contract_session
    from tests.contract.test_event_repository_contract import (
        assert_event_query_filters_run_before_limit,
    )
    from tests.contract.test_session_repository_contract import (
        assert_email_chat_visibility_preserves_owner_messages,
        assert_session_index_filters_before_pagination,
    )

    async with (
        build(settings=database_settings(), principal=principal()) as app,
        app.uow_factory() as uow,
    ):
        await assert_session_index_filters_before_pagination(uow.sessions)
        await assert_email_chat_visibility_preserves_owner_messages(uow.sessions, uow.events)
        await uow.sessions.create(contract_session())
        await assert_event_query_filters_run_before_limit(uow.events)


async def test_postgres_task_admission_query_preserves_unsettled_reservations() -> None:
    from tests.contract.test_email_store_contract import (
        assert_task_admission_query_preserves_unsettled_reservations,
    )

    async with database() as engine, create_session_factory(engine)() as session:
        await configure(session)
        await assert_task_admission_query_preserves_unsettled_reservations(
            PostgresEmailStore(session)
        )


@pytest.mark.parametrize(
    "timestamp", ["invalid", "2026-01-01T00:00:00", None, "2026-02-30T00:00:00Z"]
)
@pytest.mark.parametrize("time_zone", ["UTC", "America/New_York"])
async def test_invalid_retained_email_date_raises_controlled_validation_error(
    timestamp: str | None, time_zone: str
) -> None:
    from datetime import timedelta

    from sqlalchemy import insert

    from agent_core.adapters.persistence.mappers import email_record_values
    from tests.contract.support import NOW

    async with database() as engine, create_session_factory(engine)() as session:
        await configure(session)
        await session.execute(
            text("SELECT set_config('TimeZone', :zone, true)"), {"zone": time_zone}
        )
        source = record("invalid-date").model_copy(
            update={
                "kind": "semantic_source",
                "payload": {"account_id": "work", "evidence_at": timestamp, "excluded": False},
            }
        )
        # Bypass put to represent data retained before validation was introduced.
        await session.execute(insert(EmailRecordRow).values(**email_record_values(source)))
        store = PostgresEmailStore(session)
        for after in [None, (NOW, "zzz")]:
            with pytest.raises(ValueError, match="invalid retained email source"):
                await store.list_semantic_window(
                    principal(),
                    account_ids=["work"],
                    since=NOW,
                    until=NOW + timedelta(days=1),
                    after=after,
                    limit=1,
                )
            # Rejection must precede a failing cast and leave the transaction usable.
            assert await session.scalar(select(1)) == 1


async def test_postgres_semantic_source_timestamps_are_validated_before_writes() -> None:
    from tests.contract.test_email_store_contract import semantic_source_timestamp_contract

    async with database() as engine, create_session_factory(engine)() as session:
        await configure(session)
        await semantic_source_timestamp_contract(PostgresEmailStore(session))
        await session.commit()
