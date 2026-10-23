"""Shared owner-record store behavior, reused by the PostgreSQL adapter."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from agent_core.adapters.persistence.records import InMemoryRecordStore
from agent_core.domain.agents import Principal
from agent_core.domain.memory import Sensitivity
from agent_core.domain.records import (
    RecordCadence,
    RecordComponent,
    RecordEntry,
    RecordSeries,
    normalize_series_name,
)
from agent_core.ports.records import Bucket, Order, RecordStore, SeriesExists, Window
from tests.contract.support import NOW, principal

_FAR = NOW + timedelta(days=3650)


def other() -> Principal:
    return principal().model_copy(update={"principal_id": "other"})


def foreign_tenant() -> Principal:
    return principal().model_copy(update={"tenant_id": "tenant-b"})


def make_series(
    owner: Principal,
    display: str = "Blood pressure",
    *,
    components: tuple[str, ...] = ("systolic", "diastolic"),
    sensitivity: Sensitivity = Sensitivity.SENSITIVE,
    cadence: RecordCadence | None = None,
) -> RecordSeries:
    return RecordSeries(
        id=uuid4(),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        name=normalize_series_name(display),
        display_name=display,
        components=tuple(RecordComponent(name=name, unit="u") for name in components),
        sensitivity=sensitivity,
        cadence=cadence,
        created_at=NOW,
        updated_at=NOW,
    )


def make_entry(
    series: RecordSeries,
    at: datetime,
    values: dict[str, str],
    *,
    key: str | None = None,
    session: UUID | None = None,
    created_at: datetime = NOW,
) -> RecordEntry:
    return RecordEntry(
        id=uuid4(),
        series_id=series.id,
        tenant_id=series.tenant_id,
        principal_id=series.principal_id,
        observed_at=at,
        values={name: Decimal(value) for name, value in values.items()},
        idempotency_key=key or str(uuid4()),
        source_session_id=session,
        source_event_id=uuid4() if session else None,
        created_at=created_at,
    )


def everything() -> Window:
    return Window(until=_FAR)


async def records_scope_isolation_contract(store: RecordStore) -> None:
    owner = principal()
    series, created = await store.create_series(make_series(owner))
    assert created
    entry = make_entry(series, NOW, {"systolic": "120", "diastolic": "80"})
    await store.append(entry)
    for foreign in (other(), foreign_tenant()):
        await _assert_isolated(store, owner, foreign, series, entry)
        stolen = entry.model_copy(
            update={
                "id": uuid4(),
                "idempotency_key": "x",
                "tenant_id": foreign.tenant_id,
                "principal_id": foreign.principal_id,
            }
        )
        with pytest.raises(ValueError, match="series"):
            await store.append(stolen)
    assert await store.count_entries(owner, series.id) == 1


async def _assert_isolated(
    store: RecordStore,
    owner: Principal,
    foreign: Principal,
    series: RecordSeries,
    entry: RecordEntry,
) -> None:
    assert await store.get_series(foreign, series.name) is None
    assert await store.count_series(foreign) == 0
    assert await store.list_series(foreign, Sensitivity.RESTRICTED) == []
    assert await store.count_entries(foreign, series.id) == 0
    assert await store.query(foreign, series.id, everything(), Order.ASC, 10) == []
    assert await store.summarize(foreign, series.id, everything()) == {}
    assert await store.bucket(foreign, series.id, everything(), Bucket.DAY, ZoneInfo("UTC")) == []
    assert await store.delete_entry(foreign, series.id, entry.id) == 0
    assert await store.delete_series(foreign, series.id) == (0, False)


async def records_create_series_contract(store: RecordStore) -> None:
    owner = principal()
    first, created = await store.create_series(make_series(owner, cadence=RecordCadence.DAILY))
    assert created
    again, created_again = await store.create_series(
        make_series(owner, cadence=RecordCadence.DAILY)
    )
    assert not created_again and again == first
    reordered, created_reordered = await store.create_series(
        make_series(owner, components=("diastolic", "systolic"), cadence=RecordCadence.DAILY)
    )
    assert not created_reordered and reordered == first
    assert [c.name for c in reordered.components] == ["systolic", "diastolic"]
    changed_unit = make_series(owner, cadence=RecordCadence.DAILY)
    changed_unit = changed_unit.model_copy(
        update={
            "components": (
                changed_unit.components[0],
                RecordComponent(name="diastolic", unit="other"),
            )
        }
    )
    with pytest.raises(SeriesExists):
        await store.create_series(changed_unit)
    with pytest.raises(SeriesExists):
        await store.create_series(make_series(owner, components=("systolic",)))
    with pytest.raises(SeriesExists):
        await store.create_series(make_series(owner, cadence=RecordCadence.WEEKLY))
    with pytest.raises(SeriesExists):
        await store.create_series(make_series(owner, sensitivity=Sensitivity.RESTRICTED))
    assert await store.count_series(owner) == 1
    assert await store.get_series(owner, first.name) == first


async def records_name_collision_contract(store: RecordStore) -> None:
    owner = principal()
    first, _ = await store.create_series(make_series(owner, "Blood  Pressure"))
    second, created = await store.create_series(make_series(owner, "blood pressure"))
    assert not created and second == first
    assert second.display_name == "Blood  Pressure"
    assert await store.count_series(owner) == 1


async def records_append_replay_contract(store: RecordStore) -> None:
    owner = principal()
    series, _ = await store.create_series(make_series(owner))
    first = make_entry(series, NOW, {"systolic": "120", "diastolic": "80"}, key="k1")
    stored, replayed = await store.append(first)
    assert not replayed and stored == first
    replay = make_entry(series, NOW, {"systolic": "999", "diastolic": "99"}, key="k1")
    again, replayed = await store.append(replay)
    assert replayed and again == first
    assert await store.count_entries(owner, series.id) == 1


async def records_find_entry_contract(store: RecordStore) -> None:
    owner = principal()
    series, _ = await store.create_series(make_series(owner))
    first = make_entry(series, NOW, {"systolic": "120", "diastolic": "80"}, key="k1")
    await store.append(first)
    assert await store.find_entry(owner, series.id, "k1") == first
    assert await store.find_entry(owner, series.id, "unknown") is None
    for foreign in (other(), foreign_tenant()):
        assert await store.find_entry(foreign, series.id, "k1") is None


async def records_component_invariant_contract(store: RecordStore) -> None:
    owner = principal()
    series, _ = await store.create_series(make_series(owner))
    for values in ({"systolic": "120"}, {"systolic": "1", "diastolic": "2", "pulse": "3"}):
        with pytest.raises(ValueError, match="component"):
            await store.append(make_entry(series, NOW, values))
    assert await store.count_entries(owner, series.id) == 0
    orphan = make_entry(series, NOW, {"systolic": "1", "diastolic": "2"}).model_copy(
        update={"series_id": uuid4()}
    )
    with pytest.raises(ValueError, match="series"):
        await store.append(orphan)


async def records_query_contract(store: RecordStore) -> None:
    owner = principal()
    series, _ = await store.create_series(make_series(owner, components=("weight",)))
    days = [NOW - timedelta(days=n) for n in (4, 3, 2, 1, 0)]
    for index, day in enumerate(days):
        await store.append(make_entry(series, day, {"weight": str(80 + index)}))
    window = Window(since=days[1], until=days[3])
    asc = await store.query(owner, series.id, window, Order.ASC, 10)
    assert [e.observed_at for e in asc] == days[1:3]
    desc = await store.query(owner, series.id, window, Order.DESC, 10)
    assert [e.observed_at for e in desc] == days[1:3][::-1]
    assert len(await store.query(owner, series.id, everything(), Order.DESC, 2)) == 2
    newest = await store.query(owner, series.id, everything(), Order.DESC, 1)
    assert newest[0].observed_at == days[4]
    early = make_entry(series, NOW + timedelta(days=1), {"weight": "1"}, created_at=NOW)
    late = make_entry(
        series, NOW + timedelta(days=1), {"weight": "2"}, created_at=NOW + timedelta(seconds=1)
    )
    await store.append(late)
    await store.append(early)
    tied = Window(since=NOW + timedelta(days=1), until=NOW + timedelta(days=2))
    ascending = await store.query(owner, series.id, tied, Order.ASC, 10)
    assert [e.id for e in ascending] == [early.id, late.id]
    descending = await store.query(owner, series.id, tied, Order.DESC, 10)
    assert [e.id for e in descending] == [late.id, early.id]


async def records_reversed_window_contract(store: RecordStore) -> None:
    owner = principal()
    series, _ = await store.create_series(make_series(owner, components=("weight",)))
    await store.append(make_entry(series, NOW, {"weight": "80"}))
    reversed_window = Window(since=NOW + timedelta(days=1), until=NOW - timedelta(days=1))
    assert await store.query(owner, series.id, reversed_window, Order.ASC, 10) == []
    assert await store.summarize(owner, series.id, reversed_window) == {}
    assert await store.bucket(owner, series.id, reversed_window, Bucket.DAY, ZoneInfo("UTC")) == []


async def records_summarize_contract(store: RecordStore) -> None:
    owner = principal()
    series, _ = await store.create_series(make_series(owner, components=("weight",)))
    for day, value in ((3, "128"), (2, "82.4"), (1, "130.1")):
        await store.append(make_entry(series, NOW - timedelta(days=day), {"weight": value}))
    summary = (await store.summarize(owner, series.id, everything()))["weight"]
    assert summary.count == 3
    assert summary.minimum == Decimal("82.4") and summary.maximum == Decimal("130.1")
    assert str(summary.mean) == "113.500000"
    assert summary.first.value == Decimal("128")
    assert summary.last.value == Decimal("130.1")
    assert summary.last.observed_at == NOW - timedelta(days=1)


async def records_bucket_contract(store: RecordStore) -> None:
    owner = principal()
    zone = ZoneInfo("America/Los_Angeles")
    series, _ = await store.create_series(make_series(owner, components=("weight",)))
    # 23:30 local on the 20th and 00:30 local on the 21st straddle a UTC day.
    local = [
        datetime(2026, 10, 20, 23, 30, tzinfo=zone),
        datetime(2026, 10, 20, 8, 0, tzinfo=zone),
        datetime(2026, 10, 21, 0, 30, tzinfo=zone),
        datetime(2026, 10, 23, 9, 0, tzinfo=zone),
    ]
    for at, value in zip(local, ("10", "20", "40", "50"), strict=True):
        await store.append(make_entry(series, at, {"weight": value}))
    rows = await store.bucket(owner, series.id, everything(), Bucket.DAY, zone)
    assert [(r.start, r.end) for r in rows] == [
        (
            datetime(2026, 10, 20, tzinfo=zone).astimezone(UTC),
            datetime(2026, 10, 21, tzinfo=zone).astimezone(UTC),
        ),
        (
            datetime(2026, 10, 21, tzinfo=zone).astimezone(UTC),
            datetime(2026, 10, 22, tzinfo=zone).astimezone(UTC),
        ),
        (
            datetime(2026, 10, 23, tzinfo=zone).astimezone(UTC),
            datetime(2026, 10, 24, tzinfo=zone).astimezone(UTC),
        ),
    ]
    first = rows[0].components["weight"]
    assert (first.count, first.minimum, first.maximum) == (2, Decimal("10"), Decimal("20"))
    assert str(first.mean) == "15.000000"
    weeks = await store.bucket(owner, series.id, everything(), Bucket.WEEK, zone)
    assert len(weeks) == 1
    assert weeks[0].start == datetime(2026, 10, 19, tzinfo=zone).astimezone(UTC)
    assert weeks[0].end == datetime(2026, 10, 26, tzinfo=zone).astimezone(UTC)
    months = await store.bucket(owner, series.id, everything(), Bucket.MONTH, zone)
    assert len(months) == 1
    assert months[0].end == datetime(2026, 11, 1, tzinfo=zone).astimezone(UTC)
    assert months[0].components["weight"].count == 4


async def records_delete_contract(store: RecordStore) -> None:
    owner = principal()
    series, _ = await store.create_series(make_series(owner, components=("weight",)))
    kept = make_entry(series, NOW, {"weight": "1"})
    gone = make_entry(series, NOW - timedelta(days=1), {"weight": "2"})
    await store.append(kept)
    await store.append(gone)
    assert await store.delete_entry(owner, series.id, gone.id) == 1
    assert await store.delete_entry(owner, series.id, gone.id) == 0
    assert await store.delete_entry(owner, uuid4(), kept.id) == 0
    assert await store.delete_series(owner, series.id) == (1, True)
    assert await store.delete_series(owner, series.id) == (0, False)
    assert await store.get_series(owner, series.name) is None
    assert await store.count_series(owner) == 0


async def records_detach_session_contract(store: RecordStore) -> None:
    owner = principal()
    series, _ = await store.create_series(make_series(owner, components=("weight",)))
    session = uuid4()
    linked = make_entry(series, NOW, {"weight": "1"}, session=session)
    plain = make_entry(series, NOW - timedelta(days=1), {"weight": "2"})
    await store.append(linked)
    await store.append(plain)
    assert await store.detach_session(session) == 1
    assert await store.detach_session(session) == 0
    rows = await store.query(owner, series.id, everything(), Order.ASC, 10)
    assert len(rows) == 2
    assert all(r.source_session_id is None and r.source_event_id is None for r in rows)
    assert {r.id for r in rows} == {linked.id, plain.id}


async def records_erase_principal_contract(store: RecordStore) -> None:
    owner, foreign = principal(), other()
    mine, _ = await store.create_series(make_series(owner, components=("weight",)))
    theirs, _ = await store.create_series(make_series(foreign, components=("weight",)))
    await store.append(make_entry(mine, NOW, {"weight": "1"}))
    await store.append(make_entry(mine, NOW - timedelta(days=1), {"weight": "2"}))
    await store.append(make_entry(theirs, NOW, {"weight": "3"}))
    assert await store.erase_principal(owner) == 3
    assert await store.count_series(owner) == 0
    assert await store.count_entries(owner, mine.id) == 0
    assert await store.count_series(foreign) == 1
    assert await store.count_entries(foreign, theirs.id) == 1
    assert await store.erase_principal(owner) == 0


async def records_list_series_contract(store: RecordStore) -> None:
    owner = principal()
    zebra, _ = await store.create_series(make_series(owner, "Zebra", components=("n",)))
    await store.create_series(
        make_series(owner, "Alpha", components=("n",), sensitivity=Sensitivity.RESTRICTED)
    )
    await store.create_series(
        make_series(owner, "Mid", components=("n",), sensitivity=Sensitivity.INTERNAL)
    )
    await store.append(make_entry(zebra, NOW - timedelta(days=2), {"n": "1"}))
    await store.append(make_entry(zebra, NOW, {"n": "2"}))
    everyone = await store.list_series(owner, Sensitivity.RESTRICTED)
    assert [s.series.name for s in everyone] == ["alpha", "mid", "zebra"]
    sensitive = await store.list_series(owner, Sensitivity.SENSITIVE)
    assert [s.series.name for s in sensitive] == ["mid", "zebra"]
    internal = await store.list_series(owner, Sensitivity.INTERNAL)
    assert [s.series.name for s in internal] == ["mid"]
    summary = sensitive[1]
    assert summary.entry_count == 2
    assert summary.first_observed_at == NOW - timedelta(days=2)
    assert summary.last_observed_at == NOW
    assert sensitive[0].entry_count == 0 and sensitive[0].first_observed_at is None


CONTRACTS = [
    records_scope_isolation_contract,
    records_create_series_contract,
    records_name_collision_contract,
    records_append_replay_contract,
    records_find_entry_contract,
    records_component_invariant_contract,
    records_query_contract,
    records_reversed_window_contract,
    records_summarize_contract,
    records_bucket_contract,
    records_delete_contract,
    records_detach_session_contract,
    records_erase_principal_contract,
    records_list_series_contract,
]


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda fn: fn.__name__)
async def test_memory_records_store_contract(contract) -> None:  # type: ignore[no-untyped-def]
    await contract(InMemoryRecordStore())
