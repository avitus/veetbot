"""In-memory and PostgreSQL schedule repositories."""

from __future__ import annotations

import builtins
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.mappers import (
    schedule_idempotency_to_domain,
    schedule_idempotency_values,
    schedule_occurrence_to_domain,
    schedule_occurrence_values,
    schedule_revision_to_domain,
    schedule_revision_values,
    schedule_to_domain,
    schedule_values,
)
from agent_core.adapters.persistence.sqlalchemy_models import (
    ScheduleIdempotencyKeyRow,
    ScheduleOccurrenceRow,
    ScheduleRevisionRow,
    ScheduleRow,
)
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.schedules import (
    OccurrenceCursor,
    OccurrenceDisposition,
    Schedule,
    ScheduleCursor,
    ScheduledRunLink,
    ScheduleIdempotencyRecord,
    ScheduleOccurrence,
    ScheduleRevision,
    ScheduleState,
)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("schedule repository requires an aware instant")
    return value.astimezone(UTC)


def _positive_limit(limit: int) -> int:
    if limit <= 0:
        raise ValueError("schedule repository limit must be positive")
    return limit


class InMemoryScheduleRepository:
    def __init__(self) -> None:
        self._schedules: dict[UUID, Schedule] = {}
        self._revisions: dict[tuple[UUID, int], ScheduleRevision] = {}
        self._occurrences: dict[UUID, ScheduleOccurrence] = {}
        self._occurrence_keys: dict[tuple[UUID, datetime], UUID] = {}
        self._idempotency: dict[tuple[str, str, str], ScheduleIdempotencyRecord] = {}

    async def create(self, schedule: Schedule, revision: ScheduleRevision) -> Schedule:
        if schedule.id != revision.schedule_id or schedule.current_revision != revision.revision:
            raise ConflictError("schedule and initial revision do not match")
        if schedule.id in self._schedules:
            raise ConflictError("schedule already exists")
        self._schedules[schedule.id] = schedule
        self._revisions[(schedule.id, revision.revision)] = revision
        return schedule

    async def get(self, schedule_id: UUID, principal: Principal) -> Schedule:
        schedule = self._schedules.get(schedule_id)
        if schedule is None or not _owned_by(schedule, principal):
            raise NotFoundError("schedule not found")
        return schedule

    async def lock(self, schedule_id: UUID, principal: Principal) -> Schedule:
        return (await self.get(schedule_id, principal)).model_copy(deep=True)

    async def get_revision(
        self, schedule_id: UUID, revision: int, principal: Principal
    ) -> ScheduleRevision:
        await self.get(schedule_id, principal)
        value = self._revisions.get((schedule_id, revision))
        if value is None:
            raise NotFoundError("schedule revision not found")
        return value

    async def list(
        self,
        principal: Principal,
        *,
        limit: int,
        cursor: ScheduleCursor | None = None,
    ) -> list[Schedule]:
        _positive_limit(limit)
        values = [value for value in self._schedules.values() if _owned_by(value, principal)]
        values.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        if cursor is not None:
            values = [
                value
                for value in values
                if (value.updated_at, value.id) < (cursor.updated_at, cursor.id)
            ]
        return values[:limit]

    async def due(self, now: datetime, limit: int) -> builtins.list[UUID]:
        now_utc = _aware_utc(now)
        _positive_limit(limit)
        due = [
            schedule
            for schedule in self._schedules.values()
            if schedule.state is ScheduleState.ACTIVE
            and schedule.next_fire_at is not None
            and schedule.next_fire_at <= now_utc
        ]
        due.sort(key=lambda item: (item.next_fire_at, item.id))
        return [item.id for item in due[:limit]]

    async def next_fire_at(self) -> datetime | None:
        values = [
            schedule.next_fire_at
            for schedule in self._schedules.values()
            if schedule.state is ScheduleState.ACTIVE and schedule.next_fire_at is not None
        ]
        return min(values, default=None)

    async def lock_due(self, schedule_id: UUID, now: datetime) -> Schedule | None:
        schedule = self._schedules.get(schedule_id)
        if (
            schedule is None
            or schedule.state is not ScheduleState.ACTIVE
            or schedule.next_fire_at is None
            or schedule.next_fire_at > _aware_utc(now)
        ):
            return None
        return schedule.model_copy(deep=True)

    async def advance(self, previous: Schedule, updated: Schedule) -> Schedule:
        current = self._schedules.get(previous.id)
        if current != previous:
            raise ConflictError("schedule changed before its decision was persisted")
        if updated.id != previous.id or updated.current_revision != previous.current_revision:
            raise ConflictError("schedule decision cannot change identity or revision")
        self._schedules[updated.id] = updated.model_copy(deep=True)
        return updated.model_copy(deep=True)

    async def replace(
        self,
        previous: Schedule,
        updated: Schedule,
        revision: ScheduleRevision | None = None,
    ) -> Schedule:
        current = self._schedules.get(previous.id)
        if current != previous:
            raise ConflictError("schedule changed before mutation")
        if updated.id != previous.id:
            raise ConflictError("schedule mutation cannot change identity")
        if revision is None:
            if updated.current_revision != previous.current_revision:
                raise ConflictError("state mutation cannot change revision")
        elif (
            revision.schedule_id != previous.id
            or revision.revision != previous.current_revision + 1
            or updated.current_revision != revision.revision
            or (previous.id, revision.revision) in self._revisions
        ):
            raise ConflictError("schedule revision mutation is inconsistent")
        if revision is not None:
            self._revisions[(previous.id, revision.revision)] = revision.model_copy(deep=True)
        self._schedules[updated.id] = updated.model_copy(deep=True)
        return updated.model_copy(deep=True)

    async def insert(self, occurrence: ScheduleOccurrence) -> ScheduleOccurrence:
        if occurrence.schedule_id not in self._schedules:
            raise NotFoundError("schedule not found")
        if (occurrence.schedule_id, occurrence.schedule_revision) not in self._revisions:
            raise NotFoundError("schedule revision not found")
        key = (occurrence.schedule_id, occurrence.nominal_fire_at)
        existing_id = self._occurrence_keys.get(key)
        if existing_id is not None:
            existing = self._occurrences[existing_id]
            if existing == occurrence:
                return existing
            raise ConflictError("schedule occurrence already exists with different content")
        if occurrence.id in self._occurrences:
            raise ConflictError("schedule occurrence identifier already exists")
        self._occurrences[occurrence.id] = occurrence
        self._occurrence_keys[key] = occurrence.id
        return occurrence

    async def get_occurrence_by_nominal(
        self, schedule_id: UUID, nominal_fire_at: datetime
    ) -> ScheduleOccurrence | None:
        occurrence_id = self._occurrence_keys.get((schedule_id, _aware_utc(nominal_fire_at)))
        if occurrence_id is None:
            return None
        return self._occurrences[occurrence_id].model_copy(deep=True)

    async def latest_occurrence_at_or_before(
        self, schedule_id: UUID, instant: datetime
    ) -> ScheduleOccurrence | None:
        boundary = _aware_utc(instant)
        values = [
            occurrence
            for occurrence in self._occurrences.values()
            if occurrence.schedule_id == schedule_id and occurrence.nominal_fire_at <= boundary
        ]
        if not values:
            return None
        return max(values, key=lambda value: (value.nominal_fire_at, value.id)).model_copy(
            deep=True
        )

    async def latest_materialized_occurrence(self, schedule_id: UUID) -> ScheduleOccurrence | None:
        values = [
            occurrence
            for occurrence in self._occurrences.values()
            if occurrence.schedule_id == schedule_id
            and occurrence.disposition is OccurrenceDisposition.MATERIALIZED
        ]
        if not values:
            return None
        return max(values, key=lambda value: (value.nominal_fire_at, value.id)).model_copy(
            deep=True
        )

    async def occurrence_by_run(self, run_id: UUID) -> ScheduledRunLink | None:
        values = [value for value in self._occurrences.values() if value.run_id == run_id]
        if not values:
            return None
        if len(values) != 1:
            raise ConflictError("run is linked to multiple schedule occurrences")
        occurrence = values[0].model_copy(deep=True)
        schedule = self._schedules[occurrence.schedule_id]
        return ScheduledRunLink(
            occurrence=occurrence,
            tenant_id=schedule.tenant_id,
            principal_id=schedule.principal_id,
        )

    async def list_occurrences(
        self,
        schedule_id: UUID,
        principal: Principal,
        *,
        limit: int,
        cursor: OccurrenceCursor | None = None,
    ) -> builtins.list[ScheduleOccurrence]:
        await self.get(schedule_id, principal)
        _positive_limit(limit)
        values = [value for value in self._occurrences.values() if value.schedule_id == schedule_id]
        values.sort(key=lambda item: (item.nominal_fire_at, item.id), reverse=True)
        if cursor is not None:
            values = [
                value
                for value in values
                if (value.nominal_fire_at, value.id) < (cursor.nominal_fire_at, cursor.id)
            ]
        return values[:limit]

    async def get_idempotency(
        self, tenant_id: str, principal_id: str, key: str
    ) -> ScheduleIdempotencyRecord | None:
        return self._idempotency.get((tenant_id, principal_id, key))

    async def create_idempotency(
        self, record: ScheduleIdempotencyRecord
    ) -> ScheduleIdempotencyRecord:
        key = (record.tenant_id, record.principal_id, record.key)
        existing = self._idempotency.get(key)
        if existing is not None:
            if existing == record:
                return existing
            raise ConflictError("schedule idempotency key reused with different request")
        schedule = self._schedules.get(record.schedule_id)
        if schedule is None or (
            schedule.tenant_id != record.tenant_id or schedule.principal_id != record.principal_id
        ):
            raise NotFoundError("schedule not found")
        self._idempotency[key] = record
        return record


class InMemoryScheduleOccurrenceRepository:
    def __init__(self, schedules: InMemoryScheduleRepository) -> None:
        self._schedules = schedules

    async def insert(self, occurrence: ScheduleOccurrence) -> ScheduleOccurrence:
        return await self._schedules.insert(occurrence)

    async def get_by_nominal(
        self, schedule_id: UUID, nominal_fire_at: datetime
    ) -> ScheduleOccurrence | None:
        return await self._schedules.get_occurrence_by_nominal(schedule_id, nominal_fire_at)

    async def latest_at_or_before(
        self, schedule_id: UUID, instant: datetime
    ) -> ScheduleOccurrence | None:
        return await self._schedules.latest_occurrence_at_or_before(schedule_id, instant)

    async def latest_materialized(self, schedule_id: UUID) -> ScheduleOccurrence | None:
        return await self._schedules.latest_materialized_occurrence(schedule_id)

    async def get_by_run(self, run_id: UUID) -> ScheduledRunLink | None:
        return await self._schedules.occurrence_by_run(run_id)

    async def list(
        self,
        schedule_id: UUID,
        principal: Principal,
        *,
        limit: int,
        cursor: OccurrenceCursor | None = None,
    ) -> list[ScheduleOccurrence]:
        return await self._schedules.list_occurrences(
            schedule_id, principal, limit=limit, cursor=cursor
        )


class InMemoryScheduleIdempotencyRepository:
    def __init__(self, schedules: InMemoryScheduleRepository) -> None:
        self._schedules = schedules

    async def get(
        self, tenant_id: str, principal_id: str, key: str
    ) -> ScheduleIdempotencyRecord | None:
        return await self._schedules.get_idempotency(tenant_id, principal_id, key)

    async def create(self, record: ScheduleIdempotencyRecord) -> ScheduleIdempotencyRecord:
        return await self._schedules.create_idempotency(record)


class PostgresScheduleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, schedule: Schedule, revision: ScheduleRevision) -> Schedule:
        if schedule.id != revision.schedule_id or schedule.current_revision != revision.revision:
            raise ConflictError("schedule and initial revision do not match")
        try:
            async with self._session.begin_nested():
                await self._session.execute(
                    pg_insert(ScheduleRow).values(**schedule_values(schedule))
                )
                await self._session.execute(
                    pg_insert(ScheduleRevisionRow).values(**schedule_revision_values(revision))
                )
        except IntegrityError as exc:
            raise ConflictError("schedule already exists") from exc
        return schedule

    async def get(self, schedule_id: UUID, principal: Principal) -> Schedule:
        row = (
            await self._session.scalars(
                select(ScheduleRow).where(
                    ScheduleRow.id == schedule_id,
                    ScheduleRow.tenant_id == principal.tenant_id,
                    ScheduleRow.principal_id == principal.principal_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("schedule not found")
        return schedule_to_domain(row)

    async def lock(self, schedule_id: UUID, principal: Principal) -> Schedule:
        row = (
            await self._session.scalars(
                select(ScheduleRow)
                .where(
                    ScheduleRow.id == schedule_id,
                    ScheduleRow.tenant_id == principal.tenant_id,
                    ScheduleRow.principal_id == principal.principal_id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("schedule not found")
        return schedule_to_domain(row)

    async def get_revision(
        self, schedule_id: UUID, revision: int, principal: Principal
    ) -> ScheduleRevision:
        row = (
            await self._session.scalars(
                select(ScheduleRevisionRow)
                .join(ScheduleRow, ScheduleRow.id == ScheduleRevisionRow.schedule_id)
                .where(
                    ScheduleRevisionRow.schedule_id == schedule_id,
                    ScheduleRevisionRow.revision == revision,
                    ScheduleRow.tenant_id == principal.tenant_id,
                    ScheduleRow.principal_id == principal.principal_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("schedule revision not found")
        return schedule_revision_to_domain(row)

    async def list(
        self,
        principal: Principal,
        *,
        limit: int,
        cursor: ScheduleCursor | None = None,
    ) -> list[Schedule]:
        statement = select(ScheduleRow).where(
            ScheduleRow.tenant_id == principal.tenant_id,
            ScheduleRow.principal_id == principal.principal_id,
        )
        if cursor is not None:
            statement = statement.where(
                or_(
                    ScheduleRow.updated_at < cursor.updated_at,
                    and_(ScheduleRow.updated_at == cursor.updated_at, ScheduleRow.id < cursor.id),
                )
            )
        rows = (
            await self._session.scalars(
                statement.order_by(ScheduleRow.updated_at.desc(), ScheduleRow.id.desc()).limit(
                    _positive_limit(limit)
                )
            )
        ).all()
        return [schedule_to_domain(row) for row in rows]

    async def due(self, now: datetime, limit: int) -> builtins.list[UUID]:
        return builtins.list(
            (
                await self._session.scalars(
                    select(ScheduleRow.id)
                    .where(
                        ScheduleRow.state == ScheduleState.ACTIVE.value,
                        ScheduleRow.next_fire_at.is_not(None),
                        ScheduleRow.next_fire_at <= _aware_utc(now),
                    )
                    .order_by(ScheduleRow.next_fire_at, ScheduleRow.id)
                    .limit(_positive_limit(limit))
                )
            ).all()
        )

    async def next_fire_at(self) -> datetime | None:
        return await self._session.scalar(
            select(func.min(ScheduleRow.next_fire_at)).where(
                ScheduleRow.state == ScheduleState.ACTIVE.value,
                ScheduleRow.next_fire_at.is_not(None),
            )
        )

    async def lock_due(self, schedule_id: UUID, now: datetime) -> Schedule | None:
        row = (
            await self._session.scalars(
                select(ScheduleRow)
                .where(
                    ScheduleRow.id == schedule_id,
                    ScheduleRow.state == ScheduleState.ACTIVE.value,
                    ScheduleRow.next_fire_at.is_not(None),
                    ScheduleRow.next_fire_at <= _aware_utc(now),
                )
                .with_for_update(skip_locked=True)
            )
        ).one_or_none()
        return None if row is None else schedule_to_domain(row)

    async def advance(self, previous: Schedule, updated: Schedule) -> Schedule:
        if updated.id != previous.id or updated.current_revision != previous.current_revision:
            raise ConflictError("schedule decision cannot change identity or revision")
        values = schedule_values(updated)
        values.pop("id")
        row = (
            await self._session.scalars(
                update(ScheduleRow)
                .where(
                    ScheduleRow.id == previous.id,
                    ScheduleRow.current_revision == previous.current_revision,
                    ScheduleRow.state == previous.state.value,
                    ScheduleRow.next_fire_at == previous.next_fire_at,
                )
                .values(**values)
                .returning(ScheduleRow)
            )
        ).one_or_none()
        if row is None:
            raise ConflictError("schedule changed before its decision was persisted")
        return schedule_to_domain(row)

    async def replace(
        self,
        previous: Schedule,
        updated: Schedule,
        revision: ScheduleRevision | None = None,
    ) -> Schedule:
        if updated.id != previous.id:
            raise ConflictError("schedule mutation cannot change identity")
        if revision is None:
            if updated.current_revision != previous.current_revision:
                raise ConflictError("state mutation cannot change revision")
        elif (
            revision.schedule_id != previous.id
            or revision.revision != previous.current_revision + 1
            or updated.current_revision != revision.revision
        ):
            raise ConflictError("schedule revision mutation is inconsistent")
        values = schedule_values(updated)
        values.pop("id")
        try:
            async with self._session.begin_nested():
                row = (
                    await self._session.scalars(
                        update(ScheduleRow)
                        .where(
                            ScheduleRow.id == previous.id,
                            ScheduleRow.tenant_id == previous.tenant_id,
                            ScheduleRow.principal_id == previous.principal_id,
                            ScheduleRow.current_revision == previous.current_revision,
                            ScheduleRow.state == previous.state.value,
                            ScheduleRow.next_fire_at == previous.next_fire_at,
                            ScheduleRow.updated_at == previous.updated_at,
                        )
                        .values(**values)
                        .returning(ScheduleRow)
                    )
                ).one_or_none()
                if row is None:
                    raise ConflictError("schedule changed before mutation")
                if revision is not None:
                    await self._session.execute(
                        pg_insert(ScheduleRevisionRow).values(**schedule_revision_values(revision))
                    )
        except IntegrityError as exc:
            raise ConflictError("schedule revision mutation is inconsistent") from exc
        return schedule_to_domain(row)

    async def insert(self, occurrence: ScheduleOccurrence) -> ScheduleOccurrence:
        statement = (
            pg_insert(ScheduleOccurrenceRow)
            .values(**schedule_occurrence_values(occurrence))
            .on_conflict_do_nothing()
            .returning(ScheduleOccurrenceRow.id)
        )
        if await self._session.scalar(statement) is not None:
            return occurrence
        row = (
            await self._session.scalars(
                select(ScheduleOccurrenceRow).where(
                    ScheduleOccurrenceRow.schedule_id == occurrence.schedule_id,
                    ScheduleOccurrenceRow.nominal_fire_at == occurrence.nominal_fire_at,
                )
            )
        ).one_or_none()
        if row is not None:
            existing = schedule_occurrence_to_domain(row)
            if existing == occurrence:
                return existing
        raise ConflictError("schedule occurrence already exists with different content")

    async def get_occurrence_by_nominal(
        self, schedule_id: UUID, nominal_fire_at: datetime
    ) -> ScheduleOccurrence | None:
        row = (
            await self._session.scalars(
                select(ScheduleOccurrenceRow).where(
                    ScheduleOccurrenceRow.schedule_id == schedule_id,
                    ScheduleOccurrenceRow.nominal_fire_at == _aware_utc(nominal_fire_at),
                )
            )
        ).one_or_none()
        return None if row is None else schedule_occurrence_to_domain(row)

    async def latest_occurrence_at_or_before(
        self, schedule_id: UUID, instant: datetime
    ) -> ScheduleOccurrence | None:
        row = (
            await self._session.scalars(
                select(ScheduleOccurrenceRow)
                .where(
                    ScheduleOccurrenceRow.schedule_id == schedule_id,
                    ScheduleOccurrenceRow.nominal_fire_at <= _aware_utc(instant),
                )
                .order_by(
                    ScheduleOccurrenceRow.nominal_fire_at.desc(),
                    ScheduleOccurrenceRow.id.desc(),
                )
                .limit(1)
            )
        ).one_or_none()
        return None if row is None else schedule_occurrence_to_domain(row)

    async def latest_materialized_occurrence(self, schedule_id: UUID) -> ScheduleOccurrence | None:
        row = (
            await self._session.scalars(
                select(ScheduleOccurrenceRow)
                .where(
                    ScheduleOccurrenceRow.schedule_id == schedule_id,
                    ScheduleOccurrenceRow.disposition == OccurrenceDisposition.MATERIALIZED.value,
                )
                .order_by(
                    ScheduleOccurrenceRow.nominal_fire_at.desc(),
                    ScheduleOccurrenceRow.id.desc(),
                )
                .limit(1)
            )
        ).one_or_none()
        return None if row is None else schedule_occurrence_to_domain(row)

    async def occurrence_by_run(self, run_id: UUID) -> ScheduledRunLink | None:
        rows = (
            await self._session.execute(
                select(
                    ScheduleOccurrenceRow,
                    ScheduleRow.tenant_id,
                    ScheduleRow.principal_id,
                )
                .join(ScheduleRow, ScheduleRow.id == ScheduleOccurrenceRow.schedule_id)
                .where(ScheduleOccurrenceRow.run_id == run_id)
            )
        ).all()
        if not rows:
            return None
        if len(rows) != 1:
            raise ConflictError("run is linked to multiple schedule occurrences")
        row, tenant_id, principal_id = rows[0]
        return ScheduledRunLink(
            occurrence=schedule_occurrence_to_domain(row),
            tenant_id=tenant_id,
            principal_id=principal_id,
        )

    async def list_occurrences(
        self,
        schedule_id: UUID,
        principal: Principal,
        *,
        limit: int,
        cursor: OccurrenceCursor | None = None,
    ) -> builtins.list[ScheduleOccurrence]:
        statement = (
            select(ScheduleOccurrenceRow)
            .join(ScheduleRow, ScheduleRow.id == ScheduleOccurrenceRow.schedule_id)
            .where(
                ScheduleOccurrenceRow.schedule_id == schedule_id,
                ScheduleRow.tenant_id == principal.tenant_id,
                ScheduleRow.principal_id == principal.principal_id,
            )
        )
        if cursor is not None:
            statement = statement.where(
                or_(
                    ScheduleOccurrenceRow.nominal_fire_at < cursor.nominal_fire_at,
                    and_(
                        ScheduleOccurrenceRow.nominal_fire_at == cursor.nominal_fire_at,
                        ScheduleOccurrenceRow.id < cursor.id,
                    ),
                )
            )
        rows = (
            await self._session.scalars(
                statement.order_by(
                    ScheduleOccurrenceRow.nominal_fire_at.desc(),
                    ScheduleOccurrenceRow.id.desc(),
                ).limit(_positive_limit(limit))
            )
        ).all()
        return [schedule_occurrence_to_domain(row) for row in rows]

    async def get_idempotency(
        self, tenant_id: str, principal_id: str, key: str
    ) -> ScheduleIdempotencyRecord | None:
        row = await self._session.get(ScheduleIdempotencyKeyRow, (tenant_id, principal_id, key))
        return None if row is None else schedule_idempotency_to_domain(row)

    async def create_idempotency(
        self, record: ScheduleIdempotencyRecord
    ) -> ScheduleIdempotencyRecord:
        statement = (
            pg_insert(ScheduleIdempotencyKeyRow)
            .values(**schedule_idempotency_values(record))
            .on_conflict_do_nothing()
            .returning(ScheduleIdempotencyKeyRow.schedule_id)
        )
        if await self._session.scalar(statement) is not None:
            return record
        existing = await self.get_idempotency(record.tenant_id, record.principal_id, record.key)
        if existing == record:
            return existing
        raise ConflictError("schedule idempotency key reused with different request")


class PostgresScheduleOccurrenceRepository:
    def __init__(self, schedules: PostgresScheduleRepository) -> None:
        self._schedules = schedules

    async def insert(self, occurrence: ScheduleOccurrence) -> ScheduleOccurrence:
        return await self._schedules.insert(occurrence)

    async def get_by_nominal(
        self, schedule_id: UUID, nominal_fire_at: datetime
    ) -> ScheduleOccurrence | None:
        return await self._schedules.get_occurrence_by_nominal(schedule_id, nominal_fire_at)

    async def latest_at_or_before(
        self, schedule_id: UUID, instant: datetime
    ) -> ScheduleOccurrence | None:
        return await self._schedules.latest_occurrence_at_or_before(schedule_id, instant)

    async def latest_materialized(self, schedule_id: UUID) -> ScheduleOccurrence | None:
        return await self._schedules.latest_materialized_occurrence(schedule_id)

    async def get_by_run(self, run_id: UUID) -> ScheduledRunLink | None:
        return await self._schedules.occurrence_by_run(run_id)

    async def list(
        self,
        schedule_id: UUID,
        principal: Principal,
        *,
        limit: int,
        cursor: OccurrenceCursor | None = None,
    ) -> list[ScheduleOccurrence]:
        return await self._schedules.list_occurrences(
            schedule_id, principal, limit=limit, cursor=cursor
        )


class PostgresScheduleIdempotencyRepository:
    def __init__(self, schedules: PostgresScheduleRepository) -> None:
        self._schedules = schedules

    async def get(
        self, tenant_id: str, principal_id: str, key: str
    ) -> ScheduleIdempotencyRecord | None:
        return await self._schedules.get_idempotency(tenant_id, principal_id, key)

    async def create(self, record: ScheduleIdempotencyRecord) -> ScheduleIdempotencyRecord:
        return await self._schedules.create_idempotency(record)


def _owned_by(schedule: Schedule, principal: Principal) -> bool:
    return (
        schedule.tenant_id == principal.tenant_id
        and schedule.principal_id == principal.principal_id
    )
