"""Owner record storage: pure summary arithmetic and the in-memory adapter."""

import asyncio
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.sqlalchemy_models import RecordEntryRow, RecordSeriesRow
from agent_core.domain.agents import Principal
from agent_core.domain.memory import SENSITIVITY_ORDER, Sensitivity
from agent_core.domain.records import (
    RecordCadence,
    RecordComponent,
    RecordEntry,
    RecordPrecision,
    RecordSeries,
    canonical_value,
)
from agent_core.ports.determinism import Clock
from agent_core.ports.records import (
    Bucket,
    BucketRow,
    BucketStats,
    ComponentSummary,
    Order,
    SeriesExists,
    SeriesSummary,
    ValueAt,
    Window,
)

_SIX_PLACES = Decimal("0.000001")


def in_window(window: Window, at: datetime) -> bool:
    return (window.since is None or at >= window.since) and at < window.until


def _mean(values: list[Decimal]) -> Decimal:
    return (sum(values, Decimal(0)) / len(values)).quantize(_SIX_PLACES, rounding=ROUND_HALF_EVEN)


def _entry_order(entry: RecordEntry) -> tuple[datetime, datetime, str]:
    return entry.observed_at, entry.created_at, str(entry.id)


def summarize_entries(entries: Iterable[RecordEntry]) -> dict[str, ComponentSummary]:
    """Per-component statistics in Decimal; shared by every adapter."""
    ordered = sorted(entries, key=_entry_order)
    names = sorted({name for entry in ordered for name in entry.values})
    result: dict[str, ComponentSummary] = {}
    for name in names:
        points = [
            (entry.observed_at, entry.values[name]) for entry in ordered if name in entry.values
        ]
        values = [value for _, value in points]
        result[name] = ComponentSummary(
            count=len(values),
            minimum=min(values),
            maximum=max(values),
            mean=_mean(values),
            first=ValueAt(value=points[0][1], observed_at=points[0][0]),
            last=ValueAt(value=points[-1][1], observed_at=points[-1][0]),
        )
    return result


def _bucket_bounds(at: datetime, bucket: Bucket, zone: ZoneInfo) -> tuple[date, date]:
    local = at.astimezone(zone).date()
    if bucket is Bucket.DAY:
        return local, local + timedelta(days=1)
    if bucket is Bucket.WEEK:
        start = local - timedelta(days=local.weekday())
        return start, start + timedelta(days=7)
    start = local.replace(day=1)
    following = (start + timedelta(days=32)).replace(day=1)
    return start, following


def bucket_entries(
    entries: Iterable[RecordEntry], bucket: Bucket, zone: ZoneInfo
) -> list[BucketRow]:
    """Local day, ISO week (Monday) or month buckets; empty buckets are omitted."""
    grouped: dict[tuple[date, date], list[RecordEntry]] = {}
    for entry in entries:
        grouped.setdefault(_bucket_bounds(entry.observed_at, bucket, zone), []).append(entry)
    rows: list[BucketRow] = []
    for (start, end), members in sorted(grouped.items()):
        components: dict[str, BucketStats] = {}
        for name, summary in summarize_entries(members).items():
            components[name] = BucketStats(
                count=summary.count,
                minimum=summary.minimum,
                maximum=summary.maximum,
                mean=summary.mean,
            )
        rows.append(
            BucketRow(
                start=datetime.combine(start, time(), tzinfo=zone).astimezone(UTC),
                end=datetime.combine(end, time(), tzinfo=zone).astimezone(UTC),
                components=components,
            )
        )
    return rows


def _declaration(series: RecordSeries) -> tuple[object, ...]:
    pairs = frozenset((component.name, component.unit) for component in series.components)
    return (pairs, series.sensitivity, series.cadence)


class InMemoryRecordStore:
    """Contract-backed owner record storage."""

    def __init__(self) -> None:
        self._series: dict[tuple[str, str], dict[UUID, RecordSeries]] = {}
        self._entries: dict[UUID, list[RecordEntry]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _scope(principal: Principal) -> tuple[str, str]:
        return principal.tenant_id, principal.principal_id

    def _owned(self, principal: Principal, series_id: UUID) -> RecordSeries | None:
        return self._series.get(self._scope(principal), {}).get(series_id)

    def _in_window(
        self, principal: Principal, series_id: UUID, window: Window
    ) -> list[RecordEntry]:
        if self._owned(principal, series_id) is None:
            return []
        return [e for e in self._entries.get(series_id, []) if in_window(window, e.observed_at)]

    async def create_series(self, series: RecordSeries) -> tuple[RecordSeries, bool]:
        async with self._lock:
            owned = self._series.setdefault((series.tenant_id, series.principal_id), {})
            for existing in owned.values():
                if existing.name == series.name:
                    if _declaration(existing) != _declaration(series):
                        raise SeriesExists(series.name)
                    return existing, False
            owned[series.id] = series
            self._entries[series.id] = []
            return series, True

    async def get_series(self, principal: Principal, name: str) -> RecordSeries | None:
        async with self._lock:
            for series in self._series.get(self._scope(principal), {}).values():
                if series.name == name:
                    return series
            return None

    async def count_series(self, principal: Principal) -> int:
        async with self._lock:
            return len(self._series.get(self._scope(principal), {}))

    async def list_series(self, principal: Principal, ceiling: Sensitivity) -> list[SeriesSummary]:
        async with self._lock:
            limit = SENSITIVITY_ORDER[ceiling]
            rows: list[SeriesSummary] = []
            owned = self._series.get(self._scope(principal), {}).values()
            for series in sorted(owned, key=lambda item: item.name):
                if SENSITIVITY_ORDER[series.sensitivity] > limit:
                    continue
                times = [entry.observed_at for entry in self._entries.get(series.id, [])]
                rows.append(
                    SeriesSummary(
                        series=series,
                        entry_count=len(times),
                        first_observed_at=min(times, default=None),
                        last_observed_at=max(times, default=None),
                    )
                )
            return rows

    async def append(self, entry: RecordEntry) -> tuple[RecordEntry, bool]:
        async with self._lock:
            series = self._series.get((entry.tenant_id, entry.principal_id), {}).get(
                entry.series_id
            )
            if series is None:
                raise ValueError("series not found for this principal")
            for stored in self._entries[series.id]:
                if stored.idempotency_key == entry.idempotency_key:
                    return stored, True
            if set(entry.values) != {component.name for component in series.components}:
                raise ValueError("entry values must be exactly the declared components")
            self._entries[series.id].append(entry)
            return entry, False

    async def count_entries(self, principal: Principal, series_id: UUID) -> int:
        async with self._lock:
            if self._owned(principal, series_id) is None:
                return 0
            return len(self._entries.get(series_id, []))

    async def query(
        self,
        principal: Principal,
        series_id: UUID,
        window: Window,
        order: Order,
        limit: int,
    ) -> list[RecordEntry]:
        async with self._lock:
            rows = sorted(
                self._in_window(principal, series_id, window),
                key=_entry_order,
                reverse=order is Order.DESC,
            )
            return rows[: max(limit, 0)]

    async def summarize(
        self, principal: Principal, series_id: UUID, window: Window
    ) -> dict[str, ComponentSummary]:
        async with self._lock:
            return summarize_entries(self._in_window(principal, series_id, window))

    async def bucket(
        self,
        principal: Principal,
        series_id: UUID,
        window: Window,
        bucket: Bucket,
        zone: ZoneInfo,
    ) -> list[BucketRow]:
        async with self._lock:
            return bucket_entries(self._in_window(principal, series_id, window), bucket, zone)

    async def delete_entry(self, principal: Principal, series_id: UUID, entry_id: UUID) -> int:
        async with self._lock:
            if self._owned(principal, series_id) is None:
                return 0
            entries = self._entries[series_id]
            kept = [entry for entry in entries if entry.id != entry_id]
            self._entries[series_id] = kept
            return len(entries) - len(kept)

    async def delete_series(self, principal: Principal, series_id: UUID) -> tuple[int, bool]:
        async with self._lock:
            if self._owned(principal, series_id) is None:
                return 0, False
            del self._series[self._scope(principal)][series_id]
            return len(self._entries.pop(series_id, [])), True

    async def detach_session(self, session_id: UUID) -> int:
        async with self._lock:
            detached = 0
            for entries in self._entries.values():
                for index, entry in enumerate(entries):
                    if entry.source_session_id == session_id:
                        entries[index] = entry.model_copy(
                            update={"source_session_id": None, "source_event_id": None}
                        )
                        detached += 1
            return detached

    async def erase_principal(self, principal: Principal) -> int:
        async with self._lock:
            removed = 0
            for series_id in self._series.pop(self._scope(principal), {}):
                removed += 1 + len(self._entries.pop(series_id, []))
            return removed


def _series_from_row(row: RecordSeriesRow) -> RecordSeries:
    return RecordSeries(
        id=row.id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        name=row.name,
        display_name=row.display_name,
        components=tuple(
            RecordComponent(name=item["name"], unit=item["unit"]) for item in row.components
        ),
        sensitivity=_SENSITIVITY_BY_ORDINAL[row.sensitivity],
        cadence=None if row.cadence is None else RecordCadence(row.cadence),
        revision=row.revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _entry_from_row(row: RecordEntryRow) -> RecordEntry:
    return RecordEntry(
        id=row.id,
        series_id=row.series_id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        observed_at=row.observed_at,
        observed_precision=RecordPrecision(row.observed_precision),
        values={name: Decimal(text) for name, text in row.values.items()},
        note=row.note,
        note_withheld=row.note_withheld,
        idempotency_key=row.idempotency_key,
        source_session_id=row.source_session_id,
        source_event_id=row.source_event_id,
        created_at=row.created_at,
    )


_SENSITIVITY_BY_ORDINAL = {ordinal: level for level, ordinal in SENSITIVITY_ORDER.items()}


class PostgresRecordStore:
    """Owner records in PostgreSQL; the unit of work owns the transaction."""

    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    @staticmethod
    def _series_scope(principal: Principal) -> list[ColumnElement[bool]]:
        return [
            RecordSeriesRow.tenant_id == principal.tenant_id,
            RecordSeriesRow.principal_id == principal.principal_id,
        ]

    @staticmethod
    def _entry_scope(principal: Principal, series_id: UUID) -> list[ColumnElement[bool]]:
        return [
            RecordEntryRow.tenant_id == principal.tenant_id,
            RecordEntryRow.principal_id == principal.principal_id,
            RecordEntryRow.series_id == series_id,
        ]

    async def _series_row(self, principal: Principal, name: str) -> RecordSeriesRow | None:
        return (
            await self._session.execute(
                select(RecordSeriesRow).where(
                    *self._series_scope(principal), RecordSeriesRow.name == name
                )
            )
        ).scalar_one_or_none()

    async def _owned_row(self, principal: Principal, series_id: UUID) -> RecordSeriesRow | None:
        return (
            await self._session.execute(
                select(RecordSeriesRow).where(
                    *self._series_scope(principal), RecordSeriesRow.id == series_id
                )
            )
        ).scalar_one_or_none()

    async def create_series(self, series: RecordSeries) -> tuple[RecordSeries, bool]:
        owner = Principal(tenant_id=series.tenant_id, principal_id=series.principal_id)
        for attempt in range(2):
            existing = await self._series_row(owner, series.name)
            if existing is not None:
                stored = _series_from_row(existing)
                if _declaration(stored) != _declaration(series):
                    raise SeriesExists(series.name)
                return stored, False
            try:
                async with self._session.begin_nested():
                    self._session.add(
                        RecordSeriesRow(
                            tenant_id=series.tenant_id,
                            principal_id=series.principal_id,
                            id=series.id,
                            name=series.name,
                            display_name=series.display_name,
                            components=[
                                {"name": item.name, "unit": item.unit} for item in series.components
                            ],
                            sensitivity=SENSITIVITY_ORDER[series.sensitivity],
                            cadence=None if series.cadence is None else series.cadence.value,
                            revision=series.revision,
                            created_at=series.created_at,
                            updated_at=series.updated_at,
                        )
                    )
                    await self._session.flush()
            except IntegrityError:
                if attempt:
                    raise
                continue
            return series, True
        raise AssertionError("unreachable")  # pragma: no cover

    async def get_series(self, principal: Principal, name: str) -> RecordSeries | None:
        row = await self._series_row(principal, name)
        return None if row is None else _series_from_row(row)

    async def count_series(self, principal: Principal) -> int:
        return int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(RecordSeriesRow)
                    .where(*self._series_scope(principal))
                )
            ).scalar_one()
        )

    async def list_series(self, principal: Principal, ceiling: Sensitivity) -> list[SeriesSummary]:
        rows = (
            (
                await self._session.execute(
                    select(RecordSeriesRow)
                    .where(
                        *self._series_scope(principal),
                        RecordSeriesRow.sensitivity <= SENSITIVITY_ORDER[ceiling],
                    )
                    .order_by(RecordSeriesRow.name.collate("C"))
                )
            )
            .scalars()
            .all()
        )
        stats = {
            series_id: (count, first, last)
            for series_id, count, first, last in (
                await self._session.execute(
                    select(
                        RecordEntryRow.series_id,
                        func.count(),
                        func.min(RecordEntryRow.observed_at),
                        func.max(RecordEntryRow.observed_at),
                    )
                    .where(
                        RecordEntryRow.tenant_id == principal.tenant_id,
                        RecordEntryRow.principal_id == principal.principal_id,
                    )
                    .group_by(RecordEntryRow.series_id)
                )
            ).all()
        }
        summaries: list[SeriesSummary] = []
        for row in rows:
            count, first, last = stats.get(row.id, (0, None, None))
            summaries.append(
                SeriesSummary(
                    series=_series_from_row(row),
                    entry_count=count,
                    first_observed_at=first,
                    last_observed_at=last,
                )
            )
        return summaries

    async def _replay(self, entry: RecordEntry) -> RecordEntry | None:
        row = (
            await self._session.execute(
                select(RecordEntryRow).where(
                    RecordEntryRow.tenant_id == entry.tenant_id,
                    RecordEntryRow.principal_id == entry.principal_id,
                    RecordEntryRow.series_id == entry.series_id,
                    RecordEntryRow.idempotency_key == entry.idempotency_key,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _entry_from_row(row)

    async def append(self, entry: RecordEntry) -> tuple[RecordEntry, bool]:
        owner = Principal(tenant_id=entry.tenant_id, principal_id=entry.principal_id)
        series_row = await self._owned_row(owner, entry.series_id)
        if series_row is None:
            raise ValueError("series not found for this principal")
        stored = await self._replay(entry)
        if stored is not None:
            return stored, True
        if set(entry.values) != {item["name"] for item in series_row.components}:
            raise ValueError("entry values must be exactly the declared components")
        try:
            async with self._session.begin_nested():
                self._session.add(
                    RecordEntryRow(
                        tenant_id=entry.tenant_id,
                        principal_id=entry.principal_id,
                        id=entry.id,
                        series_id=entry.series_id,
                        observed_at=entry.observed_at,
                        observed_precision=entry.observed_precision.value,
                        values={name: canonical_value(v) for name, v in entry.values.items()},
                        note=entry.note,
                        note_withheld=entry.note_withheld,
                        idempotency_key=entry.idempotency_key,
                        source_session_id=entry.source_session_id,
                        source_event_id=entry.source_event_id,
                        created_at=entry.created_at,
                    )
                )
                await self._session.flush()
        except IntegrityError as exc:
            stored = await self._replay(entry)
            if stored is not None:
                return stored, True
            raise ValueError("series not found for this principal") from exc
        return entry, False

    async def count_entries(self, principal: Principal, series_id: UUID) -> int:
        return int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(RecordEntryRow)
                    .where(*self._entry_scope(principal, series_id))
                )
            ).scalar_one()
        )

    def _window_clauses(self, window: Window) -> list[ColumnElement[bool]]:
        clauses = [RecordEntryRow.observed_at < window.until]
        if window.since is not None:
            clauses.append(RecordEntryRow.observed_at >= window.since)
        return clauses

    async def _in_window(
        self, principal: Principal, series_id: UUID, window: Window
    ) -> list[RecordEntry]:
        rows = (
            (
                await self._session.execute(
                    select(RecordEntryRow).where(
                        *self._entry_scope(principal, series_id), *self._window_clauses(window)
                    )
                )
            )
            .scalars()
            .all()
        )
        return [_entry_from_row(row) for row in rows]

    async def query(
        self,
        principal: Principal,
        series_id: UUID,
        window: Window,
        order: Order,
        limit: int,
    ) -> list[RecordEntry]:
        keys = (RecordEntryRow.observed_at, RecordEntryRow.created_at, RecordEntryRow.id)
        ordering = [key.desc() if order is Order.DESC else key.asc() for key in keys]
        rows = (
            (
                await self._session.execute(
                    select(RecordEntryRow)
                    .where(*self._entry_scope(principal, series_id), *self._window_clauses(window))
                    .order_by(*ordering)
                    .limit(max(limit, 0))
                )
            )
            .scalars()
            .all()
        )
        return [_entry_from_row(row) for row in rows]

    async def summarize(
        self, principal: Principal, series_id: UUID, window: Window
    ) -> dict[str, ComponentSummary]:
        return summarize_entries(await self._in_window(principal, series_id, window))

    async def bucket(
        self,
        principal: Principal,
        series_id: UUID,
        window: Window,
        bucket: Bucket,
        zone: ZoneInfo,
    ) -> list[BucketRow]:
        return bucket_entries(await self._in_window(principal, series_id, window), bucket, zone)

    async def delete_entry(self, principal: Principal, series_id: UUID, entry_id: UUID) -> int:
        result = await self._session.execute(
            delete(RecordEntryRow).where(
                *self._entry_scope(principal, series_id), RecordEntryRow.id == entry_id
            )
        )
        return int(result.rowcount)  # type: ignore[attr-defined]

    async def delete_series(self, principal: Principal, series_id: UUID) -> tuple[int, bool]:
        if await self._owned_row(principal, series_id) is None:
            return 0, False
        entries = await self.count_entries(principal, series_id)
        await self._session.execute(
            delete(RecordSeriesRow).where(
                *self._series_scope(principal), RecordSeriesRow.id == series_id
            )
        )
        return entries, True

    async def detach_session(self, session_id: UUID) -> int:
        result = await self._session.execute(
            update(RecordEntryRow)
            .where(RecordEntryRow.source_session_id == session_id)
            .values(source_session_id=None, source_event_id=None)
        )
        return int(result.rowcount)  # type: ignore[attr-defined]

    async def erase_principal(self, principal: Principal) -> int:
        entries = await self._session.execute(
            delete(RecordEntryRow).where(
                RecordEntryRow.tenant_id == principal.tenant_id,
                RecordEntryRow.principal_id == principal.principal_id,
            )
        )
        series = await self._session.execute(
            delete(RecordSeriesRow).where(*self._series_scope(principal))
        )
        return int(entries.rowcount) + int(series.rowcount)  # type: ignore[attr-defined]
