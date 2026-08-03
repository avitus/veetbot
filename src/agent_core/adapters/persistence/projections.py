"""Watermarked session-history and trajectory projection scaffolds."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.conversation import (
    CONVERSATION_ADAPTER,
    conversation_items,
)
from agent_core.adapters.persistence.mappers import event_to_domain
from agent_core.adapters.persistence.sqlalchemy_models import (
    EventRow,
    ProjectionWatermarkRow,
    SessionHistoryItemRow,
    TrajectoryProjectionRow,
)
from agent_core.adapters.persistence.upcasters import EventUpcasterRegistry
from agent_core.domain.events import EventEnvelope
from agent_core.domain.persistence import ProjectionCursor, SessionHistory, TrajectoryProjection
from agent_core.ports.determinism import Clock

SESSION_HISTORY_NAME = "session_history"
SESSION_HISTORY_VERSION = "session-history@1"
TRAJECTORY_NAME = "trajectory_export"
TRAJECTORY_VERSION = "trajectory@1"
TERMINAL_EVENTS = frozenset({"run.completed", "run.failed", "run.cancelled"})
PROJECTION_BATCH_SIZE = 500


class PostgresSessionHistoryRepository:
    name = SESSION_HISTORY_NAME
    builder_version = SESSION_HISTORY_VERSION

    def __init__(
        self,
        session: AsyncSession,
        clock: Clock,
        upcasters: EventUpcasterRegistry,
    ) -> None:
        self._session = session
        self._clock = clock
        self._upcasters = upcasters

    async def _cursor(self, session_id: UUID) -> ProjectionCursor:
        scope = str(session_id)
        row = await self._session.get(ProjectionWatermarkRow, (self.name, scope))
        if row is None:
            return ProjectionCursor(
                projection_name=self.name,
                scope=scope,
                watermark_seq=0,
                builder_version=self.builder_version,
                updated_at=self._clock.now(),
            )
        return ProjectionCursor(
            projection_name=row.projection_name,
            scope=row.scope,
            watermark_seq=row.watermark_seq,
            builder_version=row.builder_version,
            updated_at=row.updated_at,
        )

    async def apply(
        self, events: Sequence[EventEnvelope], cursor: ProjectionCursor
    ) -> ProjectionCursor:
        watermark = cursor.watermark_seq
        session_id = UUID(cursor.scope)
        inserts: list[dict[str, Any]] = []
        for event in sorted(events, key=lambda item: (item.sequence, item.id)):
            if event.sequence <= watermark:
                continue
            for index, item in enumerate(conversation_items(event)):
                inserts.append(
                    {
                        "session_id": session_id,
                        "sequence": event.sequence,
                        "item_index": index,
                        "item": CONVERSATION_ADAPTER.dump_python(item, mode="json"),
                        "builder_version": self.builder_version,
                    }
                )
            watermark = event.sequence
        if inserts:
            await self._session.execute(
                pg_insert(SessionHistoryItemRow).values(inserts).on_conflict_do_nothing()
            )
        updated = ProjectionCursor(
            projection_name=self.name,
            scope=cursor.scope,
            watermark_seq=watermark,
            builder_version=self.builder_version,
            updated_at=self._clock.now(),
        )
        await self._session.execute(
            pg_insert(ProjectionWatermarkRow)
            .values(**updated.model_dump())
            .on_conflict_do_update(
                index_elements=[
                    ProjectionWatermarkRow.projection_name,
                    ProjectionWatermarkRow.scope,
                ],
                set_={
                    "watermark_seq": updated.watermark_seq,
                    "builder_version": updated.builder_version,
                    "updated_at": updated.updated_at,
                },
                where=ProjectionWatermarkRow.watermark_seq <= updated.watermark_seq,
            )
        )
        return updated

    async def catch_up(self, session_id: UUID) -> SessionHistory:
        cursor = await self._cursor(session_id)
        if cursor.builder_version != self.builder_version:
            return await self.rebuild(session_id)
        while True:
            rows = (
                await self._session.scalars(
                    select(EventRow)
                    .where(
                        EventRow.session_id == session_id,
                        EventRow.sequence > cursor.watermark_seq,
                    )
                    .order_by(EventRow.sequence, EventRow.id)
                    .limit(PROJECTION_BATCH_SIZE)
                )
            ).all()
            if not rows:
                break
            events = [event_to_domain(row, self._upcasters) for row in rows]
            cursor = await self.apply(events, cursor)
        return await self.read(session_id)

    async def rebuild(self, session_id: UUID) -> SessionHistory:
        scope = str(session_id)
        await self._session.execute(
            delete(SessionHistoryItemRow).where(SessionHistoryItemRow.session_id == session_id)
        )
        await self._session.execute(
            delete(ProjectionWatermarkRow).where(
                ProjectionWatermarkRow.projection_name == self.name,
                ProjectionWatermarkRow.scope == scope,
            )
        )
        return await self.catch_up(session_id)

    async def read(self, session_id: UUID, through_sequence: int | None = None) -> SessionHistory:
        cursor = await self._cursor(session_id)
        if cursor.builder_version != self.builder_version:
            await self.rebuild(session_id)
            cursor = await self._cursor(session_id)
        statement = select(SessionHistoryItemRow).where(
            SessionHistoryItemRow.session_id == session_id
        )
        if through_sequence is not None:
            statement = statement.where(SessionHistoryItemRow.sequence <= through_sequence)
        rows = (
            await self._session.scalars(
                statement.order_by(SessionHistoryItemRow.sequence, SessionHistoryItemRow.item_index)
            )
        ).all()
        return SessionHistory(
            session_id=session_id,
            through_sequence=(
                min(through_sequence, cursor.watermark_seq)
                if through_sequence is not None
                else cursor.watermark_seq
            ),
            items=[CONVERSATION_ADAPTER.validate_python(row.item) for row in rows],
            builder_version=cursor.builder_version,
        )


class PostgresTrajectoryProjectionRepository:
    name = TRAJECTORY_NAME
    builder_version = TRAJECTORY_VERSION

    def __init__(
        self,
        session: AsyncSession,
        clock: Clock,
        upcasters: EventUpcasterRegistry,
    ) -> None:
        self._session = session
        self._clock = clock
        self._upcasters = upcasters

    async def _cursor(self, run_id: UUID) -> ProjectionCursor:
        scope = str(run_id)
        row = await self._session.get(ProjectionWatermarkRow, (self.name, scope))
        if row is None:
            return ProjectionCursor(
                projection_name=self.name,
                scope=scope,
                watermark_seq=0,
                builder_version=self.builder_version,
                updated_at=self._clock.now(),
            )
        return ProjectionCursor(
            projection_name=row.projection_name,
            scope=row.scope,
            watermark_seq=row.watermark_seq,
            builder_version=row.builder_version,
            updated_at=row.updated_at,
        )

    async def apply(
        self, events: Sequence[EventEnvelope], cursor: ProjectionCursor
    ) -> ProjectionCursor:
        run_id = UUID(cursor.scope)
        ordered = sorted(
            (
                event
                for event in events
                if event.run_id == run_id and event.sequence > cursor.watermark_seq
            ),
            key=lambda event: (event.sequence, event.id),
        )
        if not ordered:
            return cursor
        current = await self._session.get(TrajectoryProjectionRow, run_id)
        first_sequence = ordered[0].sequence
        terminal = any(event.event_type in TERMINAL_EVENTS for event in ordered)
        if current is not None:
            first_sequence = min(first_sequence, current.first_sequence)
            terminal = terminal or current.terminal
        state = TrajectoryProjection(
            run_id=run_id,
            first_sequence=first_sequence,
            last_sequence=ordered[-1].sequence,
            terminal=terminal,
            builder_version=self.builder_version,
            updated_at=ordered[-1].created_at,
        )
        await self._session.execute(
            pg_insert(TrajectoryProjectionRow)
            .values(**state.model_dump())
            .on_conflict_do_update(
                index_elements=[TrajectoryProjectionRow.run_id],
                set_={
                    "first_sequence": func.least(
                        TrajectoryProjectionRow.first_sequence,
                        state.first_sequence,
                    ),
                    "last_sequence": state.last_sequence,
                    "terminal": TrajectoryProjectionRow.terminal | state.terminal,
                    "builder_version": state.builder_version,
                    "updated_at": state.updated_at,
                },
                where=TrajectoryProjectionRow.last_sequence <= state.last_sequence,
            )
        )
        updated = ProjectionCursor(
            projection_name=self.name,
            scope=cursor.scope,
            watermark_seq=ordered[-1].sequence,
            builder_version=self.builder_version,
            updated_at=ordered[-1].created_at,
        )
        await self._session.execute(
            pg_insert(ProjectionWatermarkRow)
            .values(**updated.model_dump())
            .on_conflict_do_update(
                index_elements=[
                    ProjectionWatermarkRow.projection_name,
                    ProjectionWatermarkRow.scope,
                ],
                set_={
                    "watermark_seq": updated.watermark_seq,
                    "builder_version": updated.builder_version,
                    "updated_at": updated.updated_at,
                },
                where=ProjectionWatermarkRow.watermark_seq <= updated.watermark_seq,
            )
        )
        return updated

    async def catch_up(self, run_id: UUID) -> TrajectoryProjection | None:
        cursor = await self._cursor(run_id)
        if cursor.builder_version != self.builder_version:
            return await self.rebuild(run_id)
        while True:
            rows = (
                await self._session.scalars(
                    select(EventRow)
                    .where(
                        EventRow.run_id == run_id,
                        EventRow.sequence > cursor.watermark_seq,
                    )
                    .order_by(EventRow.sequence, EventRow.id)
                    .limit(PROJECTION_BATCH_SIZE)
                )
            ).all()
            if not rows:
                break
            events = [event_to_domain(row, self._upcasters) for row in rows]
            cursor = await self.apply(events, cursor)
        return await self.read(run_id)

    async def rebuild(self, run_id: UUID) -> TrajectoryProjection | None:
        await self._session.execute(
            delete(TrajectoryProjectionRow).where(TrajectoryProjectionRow.run_id == run_id)
        )
        await self._session.execute(
            delete(ProjectionWatermarkRow).where(
                ProjectionWatermarkRow.projection_name == self.name,
                ProjectionWatermarkRow.scope == str(run_id),
            )
        )
        return await self.catch_up(run_id)

    async def read(self, run_id: UUID) -> TrajectoryProjection | None:
        row = await self._session.get(TrajectoryProjectionRow, run_id)
        if row is None:
            return None
        if row.builder_version != self.builder_version:
            return await self.rebuild(run_id)
        return TrajectoryProjection(
            run_id=row.run_id,
            first_sequence=row.first_sequence,
            last_sequence=row.last_sequence,
            terminal=row.terminal,
            builder_version=row.builder_version,
            updated_at=row.updated_at,
        )
