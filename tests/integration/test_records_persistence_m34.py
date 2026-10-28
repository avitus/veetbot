"""PostgreSQL owner-record store: shared contract, row security, composite ownership."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.persistence.database import create_engine, create_session_factory
from agent_core.adapters.persistence.records import InMemoryRecordStore, PostgresRecordStore
from agent_core.adapters.persistence.session_deletions import PostgresSessionDeletionRepository
from agent_core.adapters.persistence.sqlalchemy_models import RecordEntryRow
from agent_core.bootstrap import build
from agent_core.context.estimator import canonical_json_bytes
from agent_core.domain.memory import Sensitivity
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.ports.records import Bucket, Order, RecordStore, Window
from tests.contract.support import NOW, principal
from tests.contract.test_record_store_contract import (
    CONTRACTS,
    make_entry,
    make_series,
    other,
    records_bucket_contract,
    records_delete_contract,
    records_detach_session_contract,
    records_erase_principal_contract,
    records_query_contract,
    records_summarize_contract,
)
from tests.integration.m2_support import PRINCIPAL, database_settings


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


@asynccontextmanager
async def _postgres_store() -> AsyncIterator[PostgresRecordStore]:
    engine = create_engine(database_settings().database_url)
    try:
        async with create_session_factory(engine)() as session:
            await session.execute(
                text("SELECT set_config('agent_core.tenant_id', 'tenant-a', true)")
            )
            yield PostgresRecordStore(session, FixedClock(NOW))
    finally:
        await engine.dispose()

    # Forced RLS and the scoped foreign key are asserted by the two tests above.


async def test_append_replay_postgres() -> None:
    async with _postgres_store() as store:
        series, _ = await store.create_series(make_series(principal()))
        first = make_entry(series, NOW, {"systolic": "120", "diastolic": "80"}, key="crash-key")
        stored, replayed = await store.append(first)
        assert replayed is False
        again = make_entry(series, NOW, {"systolic": "999", "diastolic": "99"}, key="crash-key")
        replay, was_replay = await store.append(again)
        assert was_replay is True
        assert replay.id == stored.id and replay.values == stored.values
        assert await store.count_entries(principal(), series.id) == 1


async def _render(store: RecordStore) -> bytes:
    owner = principal()
    series, _ = await store.create_series(make_series(owner, components=("weight", "fat")))
    zone = ZoneInfo("America/Los_Angeles")
    for index in range(40):
        await store.append(
            make_entry(
                series,
                NOW - timedelta(hours=7 * index + 1),
                {"weight": f"{80 + index * 0.37:.6f}", "fat": f"{20 - index * 0.011:.6f}"},
            )
        )
    window = Window(since=NOW - timedelta(days=9), until=NOW)
    summary = await store.summarize(owner, series.id, window)
    parts: dict[str, object] = {
        "summary": {k: v.model_dump(mode="json") for k, v in summary.items()}
    }
    for bucket in Bucket:
        rows = await store.bucket(owner, series.id, window, bucket, zone)
        parts[bucket.value] = [row.model_dump(mode="json") for row in rows]
    return canonical_json_bytes(parts)


async def test_query_determinism_postgres() -> None:
    async with _postgres_store() as store:
        first = await _render(store)
    async with _postgres_store() as store:
        second = await _render(store)
        # Half-open window at exact instants.
        owner = principal()
        series, _ = await store.create_series(make_series(owner, "Edges", components=("n",)))
        for offset in (0, 1, 2):
            await store.append(make_entry(series, NOW + timedelta(days=offset), {"n": str(offset)}))
        edge = Window(since=NOW + timedelta(days=1), until=NOW + timedelta(days=2))
        rows = await store.query(owner, series.id, edge, Order.ASC, 10)
        assert [e.observed_at for e in rows] == [NOW + timedelta(days=1)]
    for contract in (records_query_contract, records_summarize_contract, records_bucket_contract):
        async with _postgres_store() as fresh:
            await contract(fresh)
    memory = await _render(InMemoryRecordStore())
    assert first == second == memory


async def test_erasure_postgres() -> None:
    async with _postgres_store() as store:
        owner = principal()
        series, _ = await store.create_series(make_series(owner, "Doomed", components=("n",)))
        await store.append(make_entry(series, NOW, {"n": "1"}))
        assert await store.delete_series(owner, series.id) == (1, True)
        assert await store.count_entries(owner, series.id) == 0
        assert (
            await store.query(owner, series.id, Window(until=NOW + timedelta(days=1)), Order.ASC, 5)
            == []
        )
        assert await store.list_series(owner, Sensitivity.RESTRICTED) == []
    for contract in (
        records_delete_contract,
        records_detach_session_contract,
        records_erase_principal_contract,
    ):
        async with _postgres_store() as fresh:
            await contract(fresh)


async def test_postgres_session_deletion_keeps_entries_and_nulls_provenance(
    tmp_path: Path,
) -> None:
    """Gate 6's session clause through the repository that production runs."""

    settings = replace(database_settings(), artifact_root=tmp_path / "artifacts")
    owner = PRINCIPAL.model_copy(update={"principal_id": f"records-{uuid4().hex[:12]}"})
    chat = Session(
        id=uuid4(),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        agent_id=UUID("00000000-0000-0000-0000-000000000010"),
        agent_version="1.0.0",
        status=SessionStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
    )
    async with build(settings=settings, storage="postgres") as composition:
        async with composition.uow_factory() as uow:
            await uow.sessions.create(chat)
            series, _ = await uow.records.create_series(
                make_series(owner, "Kept", components=("n",))
            )
            stored, _ = await uow.records.append(
                make_entry(series, NOW, {"n": "7"}, session=chat.id)
            )
        assert stored.source_session_id == chat.id and stored.source_event_id is not None
        async with composition.uow_factory() as uow:
            assert isinstance(uow.session_deletions, PostgresSessionDeletionRepository)
            assert await uow.session_deletions.delete(chat.id, owner, NOW)
        async with composition.uow_factory() as uow:
            rows = await uow.records.query(
                owner, series.id, Window(until=NOW + timedelta(days=1)), Order.ASC, 5
            )
            assert [row.id for row in rows] == [stored.id]
            assert rows[0].source_session_id is None
            assert rows[0].source_event_id is None
            assert rows[0].values == stored.values
            await uow.records.erase_principal(owner)
