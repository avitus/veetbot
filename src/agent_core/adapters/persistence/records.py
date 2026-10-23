"""Owner record storage: pure summary arithmetic and the in-memory adapter."""

import asyncio
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import NoReturn
from uuid import UUID
from zoneinfo import ZoneInfo

from agent_core.domain.agents import Principal
from agent_core.domain.memory import SENSITIVITY_ORDER, Sensitivity
from agent_core.domain.records import RecordEntry, RecordSeries
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


def summarize_entries(entries: Iterable[RecordEntry]) -> dict[str, ComponentSummary]:
    """Per-component statistics in Decimal; shared by every adapter."""
    ordered = sorted(entries, key=lambda entry: entry.observed_at)
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
    return (series.components, series.sensitivity, series.cadence)


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
                key=lambda entry: entry.observed_at,
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


class UnavailableRecordStore:
    """Stands in for the PostgreSQL store until it lands; every call fails loudly."""

    def _fail(self) -> NoReturn:
        raise RuntimeError("PostgreSQL record store is not available yet")

    async def create_series(self, series: RecordSeries) -> tuple[RecordSeries, bool]:
        self._fail()

    async def get_series(self, principal: Principal, name: str) -> RecordSeries | None:
        self._fail()

    async def count_series(self, principal: Principal) -> int:
        self._fail()

    async def list_series(self, principal: Principal, ceiling: Sensitivity) -> list[SeriesSummary]:
        self._fail()

    async def append(self, entry: RecordEntry) -> tuple[RecordEntry, bool]:
        self._fail()

    async def count_entries(self, principal: Principal, series_id: UUID) -> int:
        self._fail()

    async def query(
        self, principal: Principal, series_id: UUID, window: Window, order: Order, limit: int
    ) -> list[RecordEntry]:
        self._fail()

    async def summarize(
        self, principal: Principal, series_id: UUID, window: Window
    ) -> dict[str, ComponentSummary]:
        self._fail()

    async def bucket(
        self, principal: Principal, series_id: UUID, window: Window, bucket: Bucket, zone: ZoneInfo
    ) -> list[BucketRow]:
        self._fail()

    async def delete_entry(self, principal: Principal, series_id: UUID, entry_id: UUID) -> int:
        self._fail()

    async def delete_series(self, principal: Principal, series_id: UUID) -> tuple[int, bool]:
        self._fail()

    async def detach_session(self, session_id: UUID) -> int:
        self._fail()

    async def erase_principal(self, principal: Principal) -> int:
        self._fail()
