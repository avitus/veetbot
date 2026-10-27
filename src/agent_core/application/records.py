"""Owner records: series declaration, appends, queries and deletion (Milestone 34)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID
from zoneinfo import ZoneInfo

from agent_core.domain.agents import Principal
from agent_core.domain.events import NewEvent
from agent_core.domain.hazards import contains_injection_pattern, contains_secret_material
from agent_core.domain.memory import SENSITIVITY_ORDER, Sensitivity
from agent_core.domain.policies import TrustLevel
from agent_core.domain.records import (
    FUTURE_SKEW,
    MAX_ENTRIES_PER_SERIES,
    MAX_SERIES_PER_PRINCIPAL,
    AppendResult,
    Bucket,
    CatalogLine,
    DeleteResult,
    ImportResult,
    Order,
    QueryResult,
    RecordCadence,
    RecordComponent,
    RecordDraft,
    RecordEntry,
    RecordFutureError,
    RecordPrecision,
    RecordsCatalog,
    RecordSeries,
    RecordTimeError,
    RecordValueError,
    SeriesSummary,
    Window,
    canonical_value,
    due_since,
    normalize_series_name,
    ordered_values,
    parse_instant,
    resolve_observed_at,
    validate_value,
)
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory
from agent_core.ports.records import SeriesExists

MAX_BUCKETS: Final = 1_000
# The early refusal divides by the longest bucket, so it never refuses a window
# that fits; the post-query row count is the authoritative 1,000-bucket check.
_MAX_BUCKET_SPAN: Final = {
    Bucket.DAY: timedelta(days=1),
    Bucket.WEEK: timedelta(days=7),
    Bucket.MONTH: timedelta(days=31),
}


class RecordsRefusal(Exception):  # noqa: N818
    """A structured refusal with a closed reason code; no row was written."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _edge(text: str, edge: str) -> datetime:
    try:
        return parse_instant(text)
    except RecordTimeError as exc:
        raise RecordsRefusal(
            "records.invalid_arguments",
            f"the window's {edge} edge must be an ISO 8601 instant with a UTC offset",
        ) from exc


class RecordsService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        *,
        max_series: int = MAX_SERIES_PER_PRINCIPAL,
        max_entries: int = MAX_ENTRIES_PER_SERIES,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
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
        units = [component.unit for component in components if component.unit is not None]
        texts = [display_name, *names, *units]
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
                id=self._ids.new_id(),
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
                    "records.series_exists", "the series exists with a different declaration"
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
        origin_trust: TrustLevel,
    ) -> AppendResult:
        if origin_trust is TrustLevel.EXTERNAL_UNTRUSTED:
            raise RecordsRefusal(
                "records.untrusted_turn",
                "this turn read external content; call records.import so the owner "
                "reviews the values on an approval card",
            )
        draft = RecordDraft(
            values=dict(values), observed_at=observed_at, precision=precision, note=note
        )
        now = self._clock.now()
        async with self._uow_factory() as uow:
            series = await self._known_series(uow, principal, series_name)
            entry = self._entry(principal, series, draft, idempotency_key, session_id, now)
            count = await uow.records.count_entries(principal, series.id)
            known = await uow.records.find_entry(principal, series.id, idempotency_key)
            if known is None and count >= self._max_entries:
                raise RecordsRefusal(
                    "records.entry_limit", f"at most {self._max_entries} entries are allowed"
                )
            stored, replayed = await self._store(uow, principal, series, entry, session_id, run_id)
            if not replayed:
                count += 1
        return AppendResult(entry=stored, series=series, entry_count=count, replayed=replayed)

    async def import_entries(
        self,
        principal: Principal,
        *,
        series_name: str,
        drafts: Sequence[RecordDraft],
        idempotency_key: str,
        session_id: UUID,
        run_id: UUID | None,
    ) -> ImportResult:
        """Store a reviewed batch whole or not at all.

        The tool is EXTERNAL_WRITE, so the matrix has already asked the owner,
        who saw every value on the card; that approval is why this path takes no
        origin trust. Every draft is validated before the first write, and each
        entry's key derives from the invocation's, so a re-invoked import
        replays rather than duplicating.
        """

        now = self._clock.now()
        async with self._uow_factory() as uow:
            series = await self._known_series(uow, principal, series_name)
            entries: list[RecordEntry] = []
            for index, draft in enumerate(drafts, start=1):
                key = f"{idempotency_key}#{index}"
                try:
                    entries.append(self._entry(principal, series, draft, key, session_id, now))
                except RecordsRefusal as exc:
                    raise RecordsRefusal(exc.code, f"reading {index}: {exc.detail}") from exc
            count = await uow.records.count_entries(principal, series.id)
            fresh = 0
            for entry in entries:
                known = await uow.records.find_entry(principal, series.id, entry.idempotency_key)
                fresh += known is None
            if count + fresh > self._max_entries:
                raise RecordsRefusal(
                    "records.entry_limit", f"at most {self._max_entries} entries are allowed"
                )
            stored: list[RecordEntry] = []
            replays = 0
            for entry in entries:
                kept, replayed = await self._store(
                    uow, principal, series, entry, session_id, run_id
                )
                stored.append(kept)
                replays += replayed
        return ImportResult(
            series=series,
            entries=stored,
            entry_count=count + len(stored) - replays,
            imported=len(stored) - replays,
            replayed=replays,
        )

    async def _known_series(
        self, uow: RepositoryUnitOfWork, principal: Principal, series_name: str
    ) -> RecordSeries:
        series = await uow.records.get_series(principal, normalize_series_name(series_name))
        if series is None:
            raise RecordsRefusal("records.series_unknown", "create the series first")
        return series

    def _entry(
        self,
        principal: Principal,
        series: RecordSeries,
        draft: RecordDraft,
        idempotency_key: str,
        session_id: UUID,
        now: datetime,
    ) -> RecordEntry:
        declared = {component.name for component in series.components}
        if set(draft.values) != declared:
            raise RecordsRefusal(
                "records.component_mismatch",
                f"values must carry exactly: {', '.join(sorted(declared))}",
            )
        try:
            checked = ordered_values(
                series, {key: validate_value(value) for key, value in draft.values.items()}
            )
            when = resolve_observed_at(draft.observed_at, draft.precision, now)
        except RecordValueError as exc:
            raise RecordsRefusal("records.value_out_of_range", str(exc)) from exc
        except RecordFutureError as exc:
            raise RecordsRefusal("records.observed_in_future", str(exc)) from exc
        kept = (draft.note.strip() or None) if draft.note is not None else None
        withheld = False
        if kept:
            if contains_secret_material(kept):
                raise RecordsRefusal("records.secret_detected", "the note matched the scan")
            if contains_injection_pattern(kept):
                kept, withheld = None, True
        return RecordEntry(
            id=self._ids.new_id(),
            series_id=series.id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            observed_at=when,
            observed_precision=draft.precision,
            values=checked,
            note=kept,
            note_withheld=withheld,
            idempotency_key=idempotency_key,
            source_session_id=session_id,
            created_at=now,
        )

    async def _store(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        series: RecordSeries,
        entry: RecordEntry,
        session_id: UUID,
        run_id: UUID | None,
    ) -> tuple[RecordEntry, bool]:
        stored, replayed = await uow.records.append(entry)
        if not replayed:
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
                        "values": {
                            k: canonical_value(v)
                            for k, v in ordered_values(series, stored.values).items()
                        },
                        "note_withheld": stored.note_withheld,
                    },
                )
            )
        return stored, replayed

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
        start = _edge(since, "since") if since else None
        end = _edge(until, "until") if until else now + FUTURE_SKEW
        window = Window(since=start, until=end)
        if (
            bucket is not None
            and start is not None
            and (end - start) / _MAX_BUCKET_SPAN[bucket] > MAX_BUCKETS
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
