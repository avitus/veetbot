"""Session-wide erasure repositories for durable and deterministic adapters."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.mappers import artifact_to_domain
from agent_core.adapters.persistence.sqlalchemy_models import (
    ArtifactRow,
    ConsolidationRunRow,
    KnowledgeDocumentRow,
    MemoryRejectionRow,
    MemoryRow,
    RecallTraceRow,
    RunRow,
    ScheduleOccurrenceRow,
    SessionDeletionArtifactRow,
    SessionDeletionRow,
    SessionRow,
    SkillRevisionRow,
)
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.runs import TERMINAL_RUN_STATUSES
from agent_core.domain.trajectory import ArtifactRef


def _deletion_artifact_ref(artifact: ArtifactRef) -> ArtifactRef:
    """Retain only fields required to locate and verify bytes during deletion."""

    return artifact.model_copy(update={"name": "deleted-artifact", "metadata": {}}, deep=True)


class PostgresSessionDeletionRepository:
    """Atomically remove a session graph and retain byte-deletion work."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def delete(self, session_id: UUID, principal: Principal, deleted_at: datetime) -> bool:
        session_row = (
            await self._session.scalars(
                select(SessionRow)
                .where(
                    SessionRow.id == session_id,
                    SessionRow.tenant_id == principal.tenant_id,
                    SessionRow.principal_id == principal.principal_id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if session_row is None:
            replay = await self._owned_tombstone(session_id, principal)
            if replay is None:
                raise NotFoundError("session not found")
            return False

        active = await self._session.scalar(
            select(RunRow.id)
            .where(
                RunRow.session_id == session_id,
                RunRow.status.not_in([status.value for status in TERMINAL_RUN_STATUSES]),
            )
            .limit(1)
        )
        if active is not None:
            raise ConflictError(
                "An active run must be stopped before deleting the conversation.",
                reason="active_run_exists",
                details={"run_id": str(active)},
            )

        artifacts = list(
            (
                await self._session.scalars(
                    select(ArtifactRow).where(ArtifactRow.session_id == session_id)
                )
            ).all()
        )
        run_ids = select(RunRow.id).where(RunRow.session_id == session_id)
        trace_ids = select(RecallTraceRow.id).where(RecallTraceRow.session_id == session_id)
        memory_ids = select(MemoryRow.id).where(
            (MemoryRow.source_session_id == session_id) | (MemoryRow.formation_run_id.in_(run_ids))
        )
        artifact_ids = [row.id for row in artifacts]

        await self._session.execute(
            update(ScheduleOccurrenceRow)
            .where(
                ScheduleOccurrenceRow.session_id == session_id,
                ScheduleOccurrenceRow.disposition == "MATERIALIZED",
                ScheduleOccurrenceRow.links_erased_at.is_(None),
            )
            .values(session_id=None, run_id=None, links_erased_at=deleted_at)
        )

        await self._session.execute(
            delete(MemoryRejectionRow).where(
                MemoryRejectionRow.trace_id.in_(trace_ids)
                | MemoryRejectionRow.belief_id.in_(memory_ids)
                | MemoryRejectionRow.replacement_id.in_(memory_ids)
            )
        )
        await self._session.execute(delete(MemoryRow).where(MemoryRow.id.in_(memory_ids)))
        await self._session.execute(
            delete(ConsolidationRunRow).where(ConsolidationRunRow.session_id == session_id)
        )
        if artifact_ids:
            await self._session.execute(
                delete(KnowledgeDocumentRow).where(
                    KnowledgeDocumentRow.source_artifact_id.in_(artifact_ids)
                )
            )
        await self._session.execute(
            update(SkillRevisionRow)
            .where(SkillRevisionRow.authored_by_run_id.in_(run_ids))
            .values(authored_by_run_id=None)
        )
        await self._session.execute(
            pg_insert(SessionDeletionRow).values(
                session_id=session_id,
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                deleted_at=deleted_at,
            )
        )
        for row in artifacts:
            artifact = _deletion_artifact_ref(artifact_to_domain(row))
            await self._session.execute(
                pg_insert(SessionDeletionArtifactRow).values(
                    session_id=session_id,
                    artifact_id=artifact.id,
                    tenant_id=principal.tenant_id,
                    artifact=artifact.model_dump(mode="json"),
                )
            )
        await self._session.execute(delete(SessionRow).where(SessionRow.id == session_id))
        return True

    async def pending_artifacts(
        self,
        session_id: UUID,
        principal: Principal,
        *,
        limit: int,
    ) -> list[ArtifactRef]:
        if await self._owned_tombstone(session_id, principal) is None:
            raise NotFoundError("session not found")
        rows = (
            await self._session.scalars(
                select(SessionDeletionArtifactRow)
                .where(SessionDeletionArtifactRow.session_id == session_id)
                .order_by(SessionDeletionArtifactRow.artifact_id)
                .limit(limit)
            )
        ).all()
        return [ArtifactRef.model_validate(row.artifact) for row in rows]

    async def acknowledge_artifact(
        self, session_id: UUID, artifact_id: UUID, principal: Principal
    ) -> None:
        if await self._owned_tombstone(session_id, principal) is None:
            raise NotFoundError("session not found")
        await self._session.execute(
            delete(SessionDeletionArtifactRow).where(
                SessionDeletionArtifactRow.session_id == session_id,
                SessionDeletionArtifactRow.artifact_id == artifact_id,
            )
        )

    async def pending_sessions(self, principal: Principal, *, limit: int) -> list[UUID]:
        return list(
            (
                await self._session.scalars(
                    select(SessionDeletionRow.session_id)
                    .join(
                        SessionDeletionArtifactRow,
                        SessionDeletionArtifactRow.session_id == SessionDeletionRow.session_id,
                    )
                    .where(
                        SessionDeletionRow.tenant_id == principal.tenant_id,
                        SessionDeletionRow.principal_id == principal.principal_id,
                    )
                    .distinct()
                    .order_by(SessionDeletionRow.session_id)
                    .limit(limit)
                )
            ).all()
        )

    async def _owned_tombstone(
        self, session_id: UUID, principal: Principal
    ) -> SessionDeletionRow | None:
        return (
            await self._session.scalars(
                select(SessionDeletionRow).where(
                    SessionDeletionRow.session_id == session_id,
                    SessionDeletionRow.tenant_id == principal.tenant_id,
                    SessionDeletionRow.principal_id == principal.principal_id,
                )
            )
        ).one_or_none()


class InMemorySessionDeletionRepository:
    """Deterministic erasure over the in-process adapter's shared maps."""

    def __init__(
        self,
        *,
        sessions: Any,
        runs: Any,
        events: Any,
        invocations: Any,
        approvals: Any,
        checkpoints: Any,
        idempotency: Any,
        usage: Any,
        trajectory_exports: Any,
        artifacts: Any,
        memories: Any,
        traces: Any,
        knowledge: Any,
    ) -> None:
        self._sessions = sessions
        self._runs = runs
        self._events = events
        self._invocations = invocations
        self._approvals = approvals
        self._checkpoints = checkpoints
        self._idempotency = idempotency
        self._usage = usage
        self._trajectory_exports = trajectory_exports
        self._artifacts = artifacts
        self._memories = memories
        self._traces = traces
        self._knowledge = knowledge
        self._lock = asyncio.Lock()
        self._tombstones: dict[UUID, tuple[str, str, datetime]] = {}
        self._pending: dict[UUID, dict[UUID, ArtifactRef]] = {}

    async def delete(self, session_id: UUID, principal: Principal, deleted_at: datetime) -> bool:
        # Every collaborator normally protects its state with its own lock. Take
        # those locks in one stable order so cross-repository discovery and
        # erasure form one deterministic in-memory transaction.
        locks = sorted(
            {
                id(lock): lock
                for lock in (
                    self._lock,
                    self._sessions._lock,
                    self._runs._lock,
                    self._events._lock,
                    self._invocations._lock,
                    self._approvals._lock,
                    self._checkpoints._lock,
                    self._idempotency._lock,
                    self._usage._lock,
                    self._trajectory_exports._lock,
                    self._artifacts._lock,
                    self._memories._lock,
                    self._traces._lock,
                    self._knowledge._lock,
                )
            }.values(),
            key=id,
        )
        for lock in locks:
            await lock.acquire()
        try:
            return self._delete_locked(session_id, principal, deleted_at)
        finally:
            for lock in reversed(locks):
                lock.release()

    def _delete_locked(self, session_id: UUID, principal: Principal, deleted_at: datetime) -> bool:
        session = self._sessions._sessions.get(session_id)
        if session is None or (
            session.tenant_id != principal.tenant_id
            or session.principal_id != principal.principal_id
        ):
            if self._tombstones.get(session_id, ())[:2] == (
                principal.tenant_id,
                principal.principal_id,
            ):
                return False
            raise NotFoundError("session not found")
        active = [
            run
            for run in self._runs._runs.values()
            if run.session_id == session_id and run.status not in TERMINAL_RUN_STATUSES
        ]
        if len(active) > 1:
            raise ConflictError("session has multiple active runs")
        if active:
            raise ConflictError(
                "An active run must be stopped before deleting the conversation.",
                reason="active_run_exists",
                details={"run_id": str(active[0].id)},
            )
        run_ids = {
            run_id for run_id, run in self._runs._runs.items() if run.session_id == session_id
        }
        artifacts = {
            artifact_id: _deletion_artifact_ref(artifact)
            for artifact_id, artifact in self._artifacts._rows.items()
            if artifact.session_id == session_id
        }
        artifacts.update(
            {
                row.artifact.id: _deletion_artifact_ref(row.artifact)
                for row in self._trajectory_exports._rows.values()
                if row.artifact.session_id == session_id
            }
        )
        artifact_ids = set(artifacts)
        trace_ids = {
            trace_id
            for trace_id, trace in self._traces._traces.items()
            if trace.session_id == session_id
        }
        memory_ids = {
            value.id
            for value in self._memories._records.values()
            if value.source_session_id == session_id or value.formation_run_id in run_ids
        }

        self._memories._records = {
            key: value
            for key, value in self._memories._records.items()
            if value.source_session_id != session_id and value.formation_run_id not in run_ids
        }
        self._memories._rejections = {
            key: value
            for key, value in self._memories._rejections.items()
            if value.trace_id not in trace_ids
            and value.belief_id not in memory_ids
            and value.replacement_id not in memory_ids
        }
        self._memories._consolidations = {
            key: value
            for key, value in self._memories._consolidations.items()
            if value.session_id != session_id
        }
        self._memories._watermarks = {
            key: value for key, value in self._memories._watermarks.items() if key[2] != session_id
        }
        self._traces._traces = {
            key: value
            for key, value in self._traces._traces.items()
            if value.session_id != session_id
        }
        removed_document_rows = {
            row_id
            for row_id, document in self._knowledge._documents.items()
            if document.source_ref.id in artifact_ids
        }
        self._knowledge._documents = {
            key: value
            for key, value in self._knowledge._documents.items()
            if key not in removed_document_rows
        }
        self._knowledge._chunks = {
            key: value
            for key, value in self._knowledge._chunks.items()
            if value.document_row_id not in removed_document_rows
        }
        self._approvals._approvals = {
            key: value
            for key, value in self._approvals._approvals.items()
            if value.run_id not in run_ids
        }
        self._approvals._actions = {
            value.action_id: key for key, value in self._approvals._approvals.items()
        }
        self._invocations._invocations = {
            key: value
            for key, value in self._invocations._invocations.items()
            if value.run_id not in run_ids
        }
        self._invocations._idempotency = {
            key: value
            for key, value in self._invocations._idempotency.items()
            if value in self._invocations._invocations
        }
        self._checkpoints._checkpoints = {
            key: value
            for key, value in self._checkpoints._checkpoints.items()
            if key not in run_ids
        }
        self._idempotency._records = {
            key: value
            for key, value in self._idempotency._records.items()
            if value.run_id not in run_ids
        }
        self._usage._calls = {
            key: value for key, value in self._usage._calls.items() if value.run_id not in run_ids
        }
        self._trajectory_exports._rows = {
            key: value
            for key, value in self._trajectory_exports._rows.items()
            if value.run_id not in run_ids
        }
        self._artifacts._rows = {
            key: value for key, value in self._artifacts._rows.items() if key not in artifact_ids
        }
        self._runs._runs = {
            key: value for key, value in self._runs._runs.items() if key not in run_ids
        }
        removed_events = self._events._events.pop(session_id, [])
        removed_event_ids = {event.id for event in removed_events}
        self._events._derived = {
            key: value
            for key, value in self._events._derived.items()
            if value.id not in removed_event_ids
        }
        self._sessions._sessions.pop(session_id, None)
        self._tombstones[session_id] = (
            principal.tenant_id,
            principal.principal_id,
            deleted_at,
        )
        self._pending[session_id] = artifacts
        return True

    async def pending_artifacts(
        self,
        session_id: UUID,
        principal: Principal,
        *,
        limit: int,
    ) -> list[ArtifactRef]:
        async with self._lock:
            self._require_tombstone(session_id, principal)
            rows = sorted(self._pending.get(session_id, {}).values(), key=lambda row: row.id.int)
            return [row.model_copy(deep=True) for row in rows[:limit]]

    async def acknowledge_artifact(
        self, session_id: UUID, artifact_id: UUID, principal: Principal
    ) -> None:
        async with self._lock:
            self._require_tombstone(session_id, principal)
            self._pending.get(session_id, {}).pop(artifact_id, None)

    async def pending_sessions(self, principal: Principal, *, limit: int) -> list[UUID]:
        async with self._lock:
            rows = [
                session_id
                for session_id, artifacts in self._pending.items()
                if artifacts
                and self._tombstones.get(session_id, ())[:2]
                == (principal.tenant_id, principal.principal_id)
            ]
            return sorted(rows, key=lambda value: value.int)[:limit]

    def _require_tombstone(self, session_id: UUID, principal: Principal) -> None:
        if self._tombstones.get(session_id, ())[:2] != (
            principal.tenant_id,
            principal.principal_id,
        ):
            raise NotFoundError("session not found")
