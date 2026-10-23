"""Owner records: series declaration, appends, queries and deletion (Milestone 34)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from agent_core.domain.agents import Principal
from agent_core.domain.events import NewEvent
from agent_core.domain.hazards import contains_injection_pattern, contains_secret_material
from agent_core.domain.memory import SENSITIVITY_ORDER, Sensitivity
from agent_core.domain.records import (
    FUTURE_SKEW,
    MAX_ENTRIES_PER_SERIES,
    MAX_SERIES_PER_PRINCIPAL,
    RecordCadence,
    RecordComponent,
    RecordEntry,
    RecordFutureError,
    RecordPrecision,
    RecordSeries,
    RecordValueError,
    canonical_value,
    due_since,
    normalize_series_name,
    resolve_observed_at,
    validate_value,
)
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.ports.records import (
    Bucket,
    BucketRow,
    ComponentSummary,
    Order,
    SeriesExists,
    SeriesSummary,
    Window,
)

MAX_BUCKETS: Final = 1_000
_MIN_BUCKET_SPAN: Final = {
    Bucket.DAY: timedelta(days=1),
    Bucket.WEEK: timedelta(days=7),
    Bucket.MONTH: timedelta(days=28),
}


class RecordsRefusal(Exception):  # noqa: N818
    """A structured refusal with a closed reason code; no row was written."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class _Result(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AppendResult(_Result):
    entry: RecordEntry
    series: RecordSeries
    entry_count: int
    replayed: bool


class QueryResult(_Result):
    series: RecordSeries
    window: Window
    entries: list[RecordEntry]
    summary: dict[str, ComponentSummary]
    buckets: list[BucketRow] | None


class DeleteResult(_Result):
    series: RecordSeries
    entries_removed: int
    series_removed: bool


class CatalogLine(_Result):
    display_name: str
    components: tuple[RecordComponent, ...]
    entry_count: int
    last_observed_at: datetime | None
    last_precision: RecordPrecision | None
    cadence: RecordCadence | None
    due_since: datetime | None


class RecordsCatalog(_Result):
    lines: tuple[CatalogLine, ...]
    omitted: int


class RecordsService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        *,
        max_series: int = MAX_SERIES_PER_PRINCIPAL,
        max_entries: int = MAX_ENTRIES_PER_SERIES,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._max_series = max_series
        self._max_entries = max_entries

    def now(self) -> datetime:
        """The composition clock, for callers that render due state."""

        return self._clock.now()

    async def list_series(
        self, principal: Principal, *, ceiling: Sensitivity = Sensitivity.RESTRICTED
    ) -> list[SeriesSummary]:
        async with self._uow_factory() as uow:
            return await uow.records.list_series(principal, ceiling)

    async def create_series(
        self,
        principal: Principal,
        *,
        display_name: str,
        components: Sequence[RecordComponent],
        sensitivity: Sensitivity | None,
        cadence: RecordCadence | None,
        session_id: UUID,
        run_id: UUID | None,
    ) -> tuple[RecordSeries, bool]:
        level = Sensitivity.SENSITIVE if sensitivity is None else sensitivity
        if SENSITIVITY_ORDER[level] < SENSITIVITY_ORDER[Sensitivity.INTERNAL]:
            raise ValueError("series sensitivity must be at least internal")
        names = [component.name for component in components]
        if len(set(names)) != len(names):
            raise RecordsRefusal("records.component_mismatch", "component names must be unique")
        texts = [display_name, *names]
        if any(contains_secret_material(text) for text in texts):
            raise RecordsRefusal("records.secret_detected", "a name matched the secret scan")
        if any(contains_injection_pattern(text) for text in texts):
            raise RecordsRefusal("records.name_rejected", "a name matched the injection scan")
        name = normalize_series_name(display_name)
        now = self._clock.now()
        async with self._uow_factory() as uow:
            existing = await uow.records.get_series(principal, name)
            if existing is None and await uow.records.count_series(principal) >= self._max_series:
                raise RecordsRefusal(
                    "records.series_limit", f"at most {self._max_series} series are allowed"
                )
            candidate = RecordSeries(
                id=uuid4(),
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                name=name,
                display_name=display_name.strip(),
                components=tuple(components),
                sensitivity=level,
                cadence=cadence,
                created_at=now,
                updated_at=now,
            )
            try:
                series, created = await uow.records.create_series(candidate)
            except SeriesExists as exc:
                raise RecordsRefusal(
                    "records.series_exists", "the series exists with different components"
                ) from exc
            if created:
                await uow.events.append(
                    self._event(
                        principal,
                        session_id,
                        run_id,
                        "records.series.created",
                        {
                            "series_id": str(series.id),
                            "name": series.name,
                            "display_name": series.display_name,
                            "components": [c.name for c in series.components],
                            "sensitivity": series.sensitivity.value,
                            "cadence": series.cadence.value if series.cadence else None,
                        },
                    )
                )
        return series, created

    async def append(
        self,
        principal: Principal,
        *,
        series_name: str,
        values: Mapping[str, Decimal],
        observed_at: str | None,
        precision: RecordPrecision,
        note: str | None,
        idempotency_key: str,
        session_id: UUID,
        run_id: UUID | None,
    ) -> AppendResult:
        name = normalize_series_name(series_name)
        now = self._clock.now()
        async with self._uow_factory() as uow:
            series = await uow.records.get_series(principal, name)
            if series is None:
                raise RecordsRefusal("records.series_unknown", "create the series first")
            declared = {component.name for component in series.components}
            if set(values) != declared:
                raise RecordsRefusal(
                    "records.component_mismatch",
                    f"values must carry exactly: {', '.join(sorted(declared))}",
                )
            try:
                checked = {key: validate_value(value) for key, value in values.items()}
                when = resolve_observed_at(observed_at, precision, now)
            except RecordValueError as exc:
                raise RecordsRefusal("records.value_out_of_range", str(exc)) from exc
            except RecordFutureError as exc:
                raise RecordsRefusal("records.observed_in_future", str(exc)) from exc
            kept = (note.strip() or None) if note is not None else None
            withheld = False
            if kept:
                if contains_secret_material(kept):
                    raise RecordsRefusal("records.secret_detected", "the note matched the scan")
                if contains_injection_pattern(kept):
                    kept, withheld = None, True
            count = await uow.records.count_entries(principal, series.id)
            known = await uow.records.find_entry(principal, series.id, idempotency_key)
            if known is None and count >= self._max_entries:
                raise RecordsRefusal(
                    "records.entry_limit", f"at most {self._max_entries} entries are allowed"
                )
            entry = RecordEntry(
                id=uuid4(),
                series_id=series.id,
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                observed_at=when,
                observed_precision=precision,
                values=checked,
                note=kept,
                note_withheld=withheld,
                idempotency_key=idempotency_key,
                source_session_id=session_id,
                created_at=now,
            )
            stored, replayed = await uow.records.append(entry)
            if not replayed:
                count += 1
                await uow.events.append(
                    self._event(
                        principal,
                        session_id,
                        run_id,
                        "records.entry.appended",
                        {
                            "series_id": str(series.id),
                            "entry_id": str(stored.id),
                            "observed_at": stored.observed_at.isoformat(),
                            "observed_precision": stored.observed_precision.value,
                            "values": {k: canonical_value(v) for k, v in stored.values.items()},
                            "note_withheld": stored.note_withheld,
                        },
                    )
                )
        return AppendResult(entry=stored, series=series, entry_count=count, replayed=replayed)

    async def query(
        self,
        principal: Principal,
        *,
        series_name: str,
        since: str | None,
        until: str | None,
        limit: int,
        order: Order,
        bucket: Bucket | None,
        zone: ZoneInfo,
    ) -> QueryResult:
        name = normalize_series_name(series_name)
        now = self._clock.now()
        start = resolve_observed_at(since, RecordPrecision.EXACT, now) if since else None
        end = resolve_observed_at(until, RecordPrecision.EXACT, now) if until else now + FUTURE_SKEW
        window = Window(since=start, until=end)
        if (
            bucket is not None
            and start is not None
            and (end - start) / _MIN_BUCKET_SPAN[bucket] > MAX_BUCKETS
        ):
            raise RecordsRefusal("records.window_too_large", "more than 1,000 buckets")
        async with self._uow_factory() as uow:
            series = await uow.records.get_series(principal, name)
            if series is None:
                raise RecordsRefusal("records.series_unknown", "no such series")
            entries = await uow.records.query(principal, series.id, window, order, limit)
            summary = await uow.records.summarize(principal, series.id, window)
            rows = (
                await uow.records.bucket(principal, series.id, window, bucket, zone)
                if bucket is not None
                else None
            )
        if rows is not None and len(rows) > MAX_BUCKETS:
            raise RecordsRefusal("records.window_too_large", "more than 1,000 buckets")
        return QueryResult(
            series=series, window=window, entries=entries, summary=summary, buckets=rows
        )

    async def delete(
        self,
        principal: Principal,
        *,
        series_name: str,
        entry_id: UUID | None,
        session_id: UUID,
        run_id: UUID | None,
    ) -> DeleteResult:
        name = normalize_series_name(series_name)
        async with self._uow_factory() as uow:
            series = await uow.records.get_series(principal, name)
            if series is None:
                raise RecordsRefusal("records.series_unknown", "no such series")
            if entry_id is not None:
                removed = await uow.records.delete_entry(principal, series.id, entry_id)
                if removed:
                    await uow.events.append(
                        self._event(
                            principal,
                            session_id,
                            run_id,
                            "records.entry.deleted",
                            {"series_id": str(series.id), "entry_id": str(entry_id)},
                        )
                    )
                return DeleteResult(series=series, entries_removed=removed, series_removed=False)
            removed, gone = await uow.records.delete_series(principal, series.id)
            if gone:
                await uow.events.append(
                    self._event(
                        principal,
                        session_id,
                        run_id,
                        "records.series.deleted",
                        {"series_id": str(series.id), "entries_removed": removed},
                    )
                )
        return DeleteResult(series=series, entries_removed=removed, series_removed=gone)

    async def catalog(
        self, principal: Principal, *, ceiling: Sensitivity, max_series: int
    ) -> RecordsCatalog | None:
        now = self._clock.now()
        async with self._uow_factory() as uow:
            summaries = sorted(
                await uow.records.list_series(principal, ceiling), key=lambda s: s.series.name
            )
            if not summaries:
                return None
            lines: list[CatalogLine] = []
            for summary in summaries[:max_series]:
                precision: RecordPrecision | None = None
                if summary.last_observed_at is not None:
                    newest = await uow.records.query(
                        principal,
                        summary.series.id,
                        Window(until=now + FUTURE_SKEW),
                        Order.DESC,
                        1,
                    )
                    precision = newest[0].observed_precision if newest else None
                lines.append(
                    CatalogLine(
                        display_name=summary.series.display_name,
                        components=summary.series.components,
                        entry_count=summary.entry_count,
                        last_observed_at=summary.last_observed_at,
                        last_precision=precision,
                        cadence=summary.series.cadence,
                        due_since=due_since(summary.series.cadence, summary.last_observed_at, now),
                    )
                )
        return RecordsCatalog(lines=tuple(lines), omitted=max(0, len(summaries) - max_series))

    @staticmethod
    def _event(
        principal: Principal,
        session_id: UUID,
        run_id: UUID | None,
        event_type: str,
        payload: dict[str, object],
    ) -> NewEvent:
        return NewEvent(
            session_id=session_id,
            run_id=run_id,
            event_type=event_type,
            actor_type="principal",
            actor_id=principal.principal_id,
            payload=payload,
        )
