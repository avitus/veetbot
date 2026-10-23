"""PostgreSQL owner-record store: shared contract, row security, composite ownership."""

from uuid import uuid4

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.persistence.database import create_engine, create_session_factory
from agent_core.adapters.persistence.records import PostgresRecordStore
from agent_core.adapters.persistence.sqlalchemy_models import RecordEntryRow
from tests.contract.support import NOW, principal
from tests.contract.test_record_store_contract import (
    CONTRACTS,
    make_entry,
    make_series,
    other,
)
from tests.integration.m2_support import database_settings


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda fn: fn.__name__)
async def test_postgres_records_store_contract(contract) -> None:  # type: ignore[no-untyped-def]
    engine = create_engine(database_settings().database_url)
    try:
        async with create_session_factory(engine)() as session:
            await session.execute(
                text("SELECT set_config('agent_core.tenant_id', 'tenant-a', true)")
            )
            await contract(PostgresRecordStore(session, FixedClock(NOW)))
    finally:
        await engine.dispose()


async def test_record_tables_force_row_level_security() -> None:
    engine = create_engine(database_settings().database_url)
    try:
        async with create_session_factory(engine)() as session:
            flags = (
                await session.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname IN ('record_series','record_entries') ORDER BY relname"
                    )
                )
            ).all()
            assert [tuple(row) for row in flags] == [
                ("record_entries", True, True),
                ("record_series", True, True),
            ]
    finally:
        await engine.dispose()


async def test_cross_owner_entry_is_refused_at_write_time() -> None:
    engine = create_engine(database_settings().database_url)
    try:
        async with create_session_factory(engine)() as session:
            await session.execute(
                text("SELECT set_config('agent_core.tenant_id', 'tenant-a', true)")
            )
            store = PostgresRecordStore(session, FixedClock(NOW))
            series, _ = await store.create_series(make_series(principal()))
            entry = make_entry(series, NOW, {"systolic": "1", "diastolic": "2"})
            with pytest.raises(IntegrityError):
                await session.execute(
                    insert(RecordEntryRow).values(
                        tenant_id="tenant-a",
                        principal_id=other().principal_id,
                        id=uuid4(),
                        series_id=series.id,
                        observed_at=entry.observed_at,
                        observed_precision="exact",
                        values={"systolic": "1.000000", "diastolic": "2.000000"},
                        note=None,
                        note_withheld=False,
                        idempotency_key="cross",
                        source_session_id=None,
                        source_event_id=None,
                        created_at=NOW,
                    )
                )
    finally:
        await engine.dispose()
