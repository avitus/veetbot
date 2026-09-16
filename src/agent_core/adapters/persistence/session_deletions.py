"""Session-wide erasure repositories for durable and deterministic adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import any_, bindparam, delete, func, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.email_erasure import (
    erase_memory_source_locked,
    erase_postgres_source,
)
from agent_core.adapters.persistence.mappers import artifact_to_domain
from agent_core.adapters.persistence.people import InMemoryPeopleStore
from agent_core.adapters.persistence.people_erasure import (
    email_cleanup_key,
    erase_memory_copies_locked,
    erase_postgres_copies,
    references,
    source_cleanup_receipts,
)
from agent_core.adapters.persistence.people_reference_index import reference_overlap
from agent_core.adapters.persistence.sqlalchemy_models import (
    ArtifactRow,
    ConsolidationRunRow,
    DelegationRow,
    KnowledgeDocumentRow,
    MemoryRejectionRow,
    MemoryRevisionRow,
    MemoryRow,
    NotificationDeliveryRow,
    NotificationOutboxRow,
    PeopleHeadRow,
    PeopleLinkRow,
    PeopleRevisionRow,
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
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import PeopleCopyCleanup, PeopleErasure, PeopleQuery, PeopleSource
from agent_core.domain.runs import TERMINAL_RUN_STATUSES
from agent_core.domain.trajectory import ArtifactRef
from agent_core.ports.memory import TraceStore
from agent_core.ports.people import PeopleStore


def _deletion_artifact_ref(artifact: ArtifactRef) -> ArtifactRef:
    """Retain only fields required to locate and verify bytes during deletion."""

    return artifact.model_copy(update={"name": "deleted-artifact", "metadata": {}}, deep=True)


class PostgresSessionDeletionRepository:
    """Atomically remove a session graph and retain byte-deletion work."""

    def __init__(self, session: AsyncSession, people: PeopleStore, traces: TraceStore) -> None:
        self._session = session
        self._people = people
        self._traces = traces

    async def _people_source_ids(self, query: PeopleQuery) -> list[UUID]:
        ids: list[UUID] = []
        while True:
            rows = await self._people.query(query)
            ids.extend(row.id for row in rows[: query.limit])
            if len(rows) <= query.limit:
                return ids
            query = query.model_copy(update={"after": rows[query.limit - 1].id})

    async def erase_people_copies(
        self,
        principal: Principal,
        record_ids: list[UUID],
        erased_at: datetime,
        *,
        run_ids: Sequence[UUID] = (),
        purge_generated: bool = True,
    ) -> PeopleCopyCleanup:
        return await erase_postgres_copies(
            self._session,
            principal,
            record_ids,
            erased_at,
            run_ids=run_ids,
            purge_generated=purge_generated,
        )

    async def _source_copy_keys(self, principal: Principal, source_ids: list[UUID]) -> list[UUID]:
        if not source_ids:
            return []
        belief_ids = (
            await self._session.scalars(
                select(PeopleRevisionRow.payload["belief_id"].astext)
                .join(
                    PeopleLinkRow,
                    (PeopleLinkRow.tenant_id == PeopleRevisionRow.tenant_id)
                    & (PeopleLinkRow.principal_id == PeopleRevisionRow.principal_id)
                    & (PeopleLinkRow.entity_id == PeopleRevisionRow.entity_id)
                    & (PeopleLinkRow.revision == PeopleRevisionRow.revision),
                )
                .where(
                    PeopleRevisionRow.tenant_id == principal.tenant_id,
                    PeopleRevisionRow.principal_id == principal.principal_id,
                    PeopleLinkRow.role == "source",
                    PeopleLinkRow.target_id
                    == any_(bindparam(None, source_ids, type_=ARRAY(PGUUID(as_uuid=True)))),
                    PeopleRevisionRow.payload["belief_id"].astext.is_not(None),
                )
            )
        ).all()
        keys = sorted({*source_ids, *(UUID(key) for key in belief_ids if key is not None)})
        traces = (
            await self._session.scalars(
                select(RecallTraceRow.id).where(
                    RecallTraceRow.tenant_id == principal.tenant_id,
                    RecallTraceRow.principal_id == principal.principal_id,
                    reference_overlap("trace::text", keys),
                )
            )
        ).all()
        return sorted({*keys, *traces})

    async def _erase_source_copies(
        self,
        principal: Principal,
        sources: list[UUID],
        keys: list[UUID],
        erased_at: datetime,
        *,
        deleted_session_id: UUID | None = None,
        request_hash: str | None = None,
    ) -> None:
        if not sources:
            return
        beliefs = list(
            (
                await self._session.scalars(
                    select(MemoryRow.id).where(
                        MemoryRow.tenant_id == principal.tenant_id,
                        MemoryRow.principal_id == principal.principal_id,
                        MemoryRow.authority == "inferred"
                        if deleted_session_id is None
                        else (
                            (MemoryRow.source_session_id == deleted_session_id)
                            | MemoryRow.formation_run_id.in_(
                                select(RunRow.id).where(RunRow.session_id == deleted_session_id)
                            )
                        ),
                        MemoryRow.id
                        == any_(bindparam(None, keys, type_=ARRAY(PGUUID(as_uuid=True)))),
                    )
                )
            ).all()
        )
        cleanup = await self.erase_people_copies(principal, keys, erased_at)
        for receipt in source_cleanup_receipts(
            principal,
            sources,
            keys,
            cleanup,
            erased_at,
            belief_ids=beliefs,
            request_hash=request_hash,
        ):
            await self._people.put(receipt, expected_revision=0)

    async def erase_call_source(
        self, principal: Principal, call_id: str, erased_at: datetime
    ) -> dict[str, int]:
        return await erase_postgres_source(
            self._session, principal, "", call_id, frozenset(), erased_at, calling=True
        )

    async def erase_email_source(
        self,
        principal: Principal,
        account_id: str,
        thread_id: str,
        message_ids: frozenset[str],
        erased_at: datetime,
    ) -> dict[str, int]:
        async with self._people.lock(principal):
            return await self._erase_email_source_with_people_lock(
                principal, account_id, thread_id, message_ids, erased_at
            )

    async def _erase_email_source_with_people_lock(
        self,
        principal: Principal,
        account_id: str,
        thread_id: str,
        message_ids: frozenset[str],
        erased_at: datetime,
    ) -> dict[str, int]:
        async with self._people.lock(principal):
            source_ids = await self._people_source_ids(
                PeopleQuery(
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    kinds=["source"],
                    sensitivity_ceiling=Sensitivity.RESTRICTED,
                    account_id=account_id,
                    thread_id=thread_id,
                    message_ids=list(message_ids),
                    limit=100,
                )
            )
            copy_keys = await self._source_copy_keys(principal, source_ids)
            result = await erase_postgres_source(
                self._session, principal, account_id, thread_id, message_ids, erased_at
            )
            cleanup_key = email_cleanup_key(principal, account_id, thread_id)
            await self._erase_source_copies(
                principal, source_ids, copy_keys, erased_at, request_hash=cleanup_key
            )
            await self._people.erase_email_source(principal, account_id, thread_id, message_ids)
            await self._traces.erase_people(principal, source_ids)
            pending, artifacts = (
                await self._session.execute(
                    select(
                        func.count(),
                        func.coalesce(
                            func.sum(
                                func.jsonb_array_length(
                                    PeopleRevisionRow.payload["pending_artifact_ids"]
                                )
                            ),
                            0,
                        ),
                    )
                    .select_from(PeopleRevisionRow)
                    .join(
                        PeopleHeadRow,
                        (PeopleHeadRow.tenant_id == PeopleRevisionRow.tenant_id)
                        & (PeopleHeadRow.principal_id == PeopleRevisionRow.principal_id)
                        & (PeopleHeadRow.id == PeopleRevisionRow.entity_id)
                        & (PeopleHeadRow.revision == PeopleRevisionRow.revision),
                    )
                    .where(
                        PeopleHeadRow.tenant_id == principal.tenant_id,
                        PeopleHeadRow.principal_id == principal.principal_id,
                        PeopleHeadRow.kind == "erasure",
                        ~PeopleHeadRow.erased,
                        PeopleRevisionRow.payload["request_hash"].astext == cleanup_key,
                        PeopleRevisionRow.payload["state"].astext == "cleanup_pending",
                    )
                )
            ).one()
            if pending:
                result["pending_people_cleanup"] = int(pending)
            result["pending_artifacts"] = max(result.get("pending_artifacts", 0), int(artifacts))
            return result

    async def delete(self, session_id: UUID, principal: Principal, deleted_at: datetime) -> bool:
        async with self._people.lock(principal):
            return await self._delete_with_people_lock(session_id, principal, deleted_at)

    async def _delete_with_people_lock(
        self, session_id: UUID, principal: Principal, deleted_at: datetime
    ) -> bool:
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

        # Delegated child sessions exist only for their parent: erase each of
        # them first; the parent's ledger rows leave with the session through
        # their CASCADE foreign keys. A child deleted alone stamps the ledger
        # row and clears its identifiers instead.
        parent_rows = (
            await self._session.scalars(
                select(DelegationRow).where(DelegationRow.parent_session_id == session_id)
            )
        ).all()
        for parent_row in parent_rows:
            for child in parent_row.children:
                child_session = child.get("child_session_id")
                if child_session is None:
                    continue
                try:
                    await self.delete(UUID(str(child_session)), principal, deleted_at)
                except NotFoundError:
                    # A dangling ledger link — the child session and its
                    # tombstone are both gone — must not block the parent.
                    continue
        referencing = (
            await self._session.scalars(
                select(DelegationRow)
                .where(DelegationRow.children.contains([{"child_session_id": str(session_id)}]))
                .with_for_update()
            )
        ).all()
        for ledger_row in referencing:
            ledger_row.children = [
                (
                    {**child, "child_run_id": None, "child_session_id": None}
                    if child.get("child_session_id") == str(session_id)
                    else child
                )
                for child in ledger_row.children
            ]
            ledger_row.links_erased_at = deleted_at

        source_ids = await self._people_source_ids(
            PeopleQuery(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                kinds=["source"],
                sensitivity_ceiling=Sensitivity.RESTRICTED,
                session_id=session_id,
                limit=100,
            )
        )
        copy_keys = await self._source_copy_keys(principal, source_ids)
        await self._erase_source_copies(
            principal, source_ids, copy_keys, deleted_at, deleted_session_id=session_id
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

        pending_notification_ids = select(NotificationOutboxRow.id).where(
            NotificationOutboxRow.session_id == session_id,
            NotificationOutboxRow.status == "pending",
        )
        await self._session.execute(
            delete(NotificationDeliveryRow).where(
                NotificationDeliveryRow.notification_id.in_(pending_notification_ids)
            )
        )
        await self._session.execute(
            delete(NotificationOutboxRow).where(
                NotificationOutboxRow.session_id == session_id,
                NotificationOutboxRow.status == "pending",
            )
        )

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
        await self._session.execute(
            delete(MemoryRevisionRow).where(
                MemoryRevisionRow.tenant_id == principal.tenant_id,
                MemoryRevisionRow.principal_id == principal.principal_id,
                MemoryRevisionRow.payload["source_session_id"].astext == str(session_id),
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
        await self._people.erase_session(principal, session_id)
        await self._traces.erase_people(principal, source_ids)
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
        people: InMemoryPeopleStore,
        episodes: Any,
        traces: Any,
        knowledge: Any,
        schedules: Any,
        notification_outbox: Any,
        delegations: Any,
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
        self._people = people
        self._episodes = episodes
        self._traces = traces
        self._knowledge = knowledge
        self._schedules = schedules
        self._notification_outbox = notification_outbox
        self._delegations = delegations
        self._lock = asyncio.Lock()
        self._tombstones: dict[UUID, tuple[str, str, datetime]] = {}
        self._pending: dict[UUID, dict[UUID, ArtifactRef]] = {}

    def _source_copy_keys_locked(self, principal: Principal, sources: list[UUID]) -> list[UUID]:
        keys = set(sources)
        for key, revisions in self._people._records.items():
            if key[:2] != (principal.tenant_id, principal.principal_id):
                continue
            for record in revisions:
                if keys.intersection(record.support_ids) and hasattr(record, "belief_id"):
                    keys.add(record.belief_id)
        identifiers = {str(key) for key in keys}
        keys.update(
            trace.id
            for trace in self._traces._traces.values()
            if (trace.tenant_id, trace.principal_id)
            == (principal.tenant_id, principal.principal_id)
            and references(trace.model_dump(mode="json"), identifiers)
        )
        return sorted(keys)

    def _erase_source_copies_locked(
        self,
        principal: Principal,
        sources: list[UUID],
        keys: list[UUID],
        erased_at: datetime,
        *,
        deleted_session_id: UUID | None = None,
        request_hash: str | None = None,
    ) -> None:
        if not sources:
            return
        key_set = set(keys)
        deleted_runs = {
            run.id for run in self._runs._runs.values() if run.session_id == deleted_session_id
        }
        beliefs = [
            record.id
            for record in self._memories._records.values()
            if (record.tenant_id, record.principal_id)
            == (principal.tenant_id, principal.principal_id)
            and record.id in key_set
            and (
                record.authority.value == "inferred"
                if deleted_session_id is None
                else record.source_session_id == deleted_session_id
                or record.formation_run_id in deleted_runs
            )
        ]
        cleanup = erase_memory_copies_locked(self, principal, keys, erased_at)
        for receipt in source_cleanup_receipts(
            principal,
            sources,
            keys,
            cleanup,
            erased_at,
            belief_ids=beliefs,
            request_hash=request_hash,
        ):
            key = (principal.tenant_id, principal.principal_id, receipt.id)
            if key in self._people._records:
                continue
            self._people._records[key] = [receipt]
            owner = (principal.tenant_id, principal.principal_id)
            self._people._positions[owner] = self._people._positions.get(owner, 0) + 1

    async def erase_people_copies(
        self,
        principal: Principal,
        record_ids: list[UUID],
        erased_at: datetime,
        *,
        run_ids: Sequence[UUID] = (),
        purge_generated: bool = True,
    ) -> PeopleCopyCleanup:
        locks = sorted(
            {
                id(lock): lock
                for lock in (
                    self._lock,
                    self._sessions._lock,
                    self._events._lock,
                    self._runs._lock,
                    self._invocations._lock,
                    self._checkpoints._lock,
                    self._artifacts._lock,
                    self._trajectory_exports._lock,
                    self._knowledge._lock,
                    self._traces._lock,
                )
            }.values(),
            key=id,
        )
        for lock in locks:
            await lock.acquire()
        try:
            return erase_memory_copies_locked(
                self, principal, record_ids, erased_at, run_ids=run_ids
            )
        finally:
            for lock in reversed(locks):
                lock.release()

    async def erase_call_source(
        self, principal: Principal, call_id: str, erased_at: datetime
    ) -> dict[str, int]:
        locks = sorted(
            {
                id(lock): lock
                for lock in (
                    self._lock,
                    self._sessions._lock,
                    self._runs._lock,
                    self._events._lock,
                    self._invocations._lock,
                    self._checkpoints._lock,
                    self._episodes._lock,
                    self._artifacts._lock,
                    self._memories._lock,
                    self._traces._lock,
                    self._trajectory_exports._lock,
                    self._knowledge._lock,
                )
            }.values(),
            key=id,
        )
        for lock in locks:
            await lock.acquire()
        try:
            return erase_memory_source_locked(
                self, principal, "", call_id, frozenset(), erased_at, calling=True
            )
        finally:
            for lock in reversed(locks):
                lock.release()

    async def erase_email_source(
        self,
        principal: Principal,
        account_id: str,
        thread_id: str,
        message_ids: frozenset[str],
        erased_at: datetime,
    ) -> dict[str, int]:
        async with self._people.lock(principal):
            return await self._erase_email_source_with_people_lock(
                principal, account_id, thread_id, message_ids, erased_at
            )

    async def _erase_email_source_with_people_lock(
        self,
        principal: Principal,
        account_id: str,
        thread_id: str,
        message_ids: frozenset[str],
        erased_at: datetime,
    ) -> dict[str, int]:
        locks = sorted(
            {
                id(lock): lock
                for lock in (
                    self._lock,
                    self._sessions._lock,
                    self._runs._lock,
                    self._events._lock,
                    self._invocations._lock,
                    self._checkpoints._lock,
                    self._episodes._lock,
                    self._artifacts._lock,
                    self._memories._lock,
                    self._traces._lock,
                    self._trajectory_exports._lock,
                    self._knowledge._lock,
                )
            }.values(),
            key=id,
        )
        for lock in locks:
            await lock.acquire()
        try:
            before = {
                key[2]
                for key in self._people._records
                if key[:2] == (principal.tenant_id, principal.principal_id)
            }
            sources = [
                row.id
                for key, versions in self._people._records.items()
                if key[:2] == (principal.tenant_id, principal.principal_id)
                and isinstance(row := versions[-1], PeopleSource)
                and row.source_kind == "email"
                and row.account_id == account_id
                and row.thread_id == thread_id
                and row.message_id in message_ids
            ]
            keys = self._source_copy_keys_locked(principal, sources)
            result = erase_memory_source_locked(
                self, principal, account_id, thread_id, message_ids, erased_at
            )
            cleanup_key = email_cleanup_key(principal, account_id, thread_id)
            self._erase_source_copies_locked(
                principal, sources, keys, erased_at, request_hash=cleanup_key
            )
            self._people.erase_email_source_locked(principal, account_id, thread_id, message_ids)
            after = {
                key[2]
                for key in self._people._records
                if key[:2] == (principal.tenant_id, principal.principal_id)
            }
            self._traces.erase_people_locked(principal, list(before - after))
            pending = [
                row
                for key, versions in self._people._records.items()
                if key[:2] == (principal.tenant_id, principal.principal_id)
                and isinstance(row := versions[-1], PeopleErasure)
                and row.request_hash == cleanup_key
                and row.state == "cleanup_pending"
            ]
            if pending:
                result["pending_people_cleanup"] = len(pending)
            result["pending_artifacts"] = max(
                result.get("pending_artifacts", 0),
                sum(len(row.pending_artifact_ids) for row in pending),
            )
            return result
        finally:
            for lock in reversed(locks):
                lock.release()

    async def delete(self, session_id: UUID, principal: Principal, deleted_at: datetime) -> bool:
        async with self._people.lock(principal):
            return await self._delete_with_people_lock(session_id, principal, deleted_at)

    async def _delete_with_people_lock(
        self, session_id: UUID, principal: Principal, deleted_at: datetime
    ) -> bool:
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
                    self._episodes._lock,
                    self._traces._lock,
                    self._knowledge._lock,
                    self._notification_outbox._lock,
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
        sources = [
            row.id
            for key, versions in self._people._records.items()
            if key[:2] == (principal.tenant_id, principal.principal_id)
            and isinstance(row := versions[-1], PeopleSource)
            and row.session_id == session_id
        ]
        keys = self._source_copy_keys_locked(principal, sources)
        self._erase_source_copies_locked(
            principal, sources, keys, deleted_at, deleted_session_id=session_id
        )
        # Delegated child sessions exist only for their parent: erase each of
        # them first, then drop the parent's ledger rows the way the CASCADE
        # foreign keys do in PostgreSQL. A child deleted alone stamps the
        # ledger row and clears its identifiers instead.
        parent_rows = [
            row for row in self._delegations._rows.values() if row.parent_session_id == session_id
        ]
        for row in parent_rows:
            for child in row.children:
                if (
                    child.child_session_id is not None
                    and child.child_session_id in self._sessions._sessions
                ):
                    self._delete_locked(child.child_session_id, principal, deleted_at)
        self._delegations._rows = {
            key: value
            for key, value in self._delegations._rows.items()
            if value.parent_session_id != session_id
        }
        for key, row in list(self._delegations._rows.items()):
            if not any(child.child_session_id == session_id for child in row.children):
                continue
            self._delegations._rows[key] = row.model_copy(
                update={
                    "children": [
                        (
                            child.model_copy(
                                update={"child_run_id": None, "child_session_id": None}
                            )
                            if child.child_session_id == session_id
                            else child
                        )
                        for child in row.children
                    ],
                    "links_erased_at": deleted_at,
                },
                deep=True,
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
        notification_rows = self._notification_outbox._notifications.items()
        pending_notification_ids = {
            notification_id
            for notification_id, notification in notification_rows
            if notification.session_id == session_id and notification.status.value == "pending"
        }
        self._notification_outbox._notifications = {
            notification_id: notification
            for notification_id, notification in notification_rows
            if notification_id not in pending_notification_ids
        }
        self._notification_outbox._dedupe_keys = {
            notification.dedupe_key
            for notification in self._notification_outbox._notifications.values()
        }
        self._notification_outbox._deliveries = {
            key: delivery
            for key, delivery in self._notification_outbox._deliveries.items()
            if delivery.notification_id not in pending_notification_ids
        }

        self._schedules._occurrences = {
            occurrence_id: (
                occurrence.model_copy(
                    update={
                        "session_id": None,
                        "run_id": None,
                        "links_erased_at": deleted_at,
                    }
                )
                if occurrence.session_id == session_id and occurrence.links_erased_at is None
                else occurrence
            )
            for occurrence_id, occurrence in self._schedules._occurrences.items()
        }

        self._memories._records = {
            key: value
            for key, value in self._memories._records.items()
            if value.source_session_id != session_id and value.formation_run_id not in run_ids
        }
        self._memories._erasure_pending.intersection_update(self._memories._records)
        self._memories._history = {
            key: [(at, record) for at, record in value if record.source_session_id != session_id]
            for key, value in self._memories._history.items()
            if key in self._memories._records
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
        removed_episode_ids = {
            key for key, value in self._episodes._records.items() if value.session_id == session_id
        }
        self._episodes._records = {
            key: value
            for key, value in self._episodes._records.items()
            if key not in removed_episode_ids
        }
        self._episodes._by_derivation = {
            key: value
            for key, value in self._episodes._by_derivation.items()
            if value not in removed_episode_ids
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
        for repository in (
            self._events,
            self._runs,
            self._invocations,
            self._checkpoints,
            self._artifacts,
            self._trajectory_exports,
        ):
            for run_id in run_ids:
                repository._people_erased_runs.pop(run_id, None)
        for run_id in run_ids:
            self._knowledge._people_erased_runs.pop(run_id, None)
            self._traces._people_erased_runs.pop(run_id, None)
        removed_events = self._events._events.pop(session_id, [])
        removed_event_ids = {event.id for event in removed_events}
        self._events._derived = {
            key: value
            for key, value in self._events._derived.items()
            if value.id not in removed_event_ids
        }
        before = {
            key[2]
            for key in self._people._records
            if key[:2] == (principal.tenant_id, principal.principal_id)
        }
        self._people.erase_session_locked(principal, session_id)
        after = {
            key[2]
            for key in self._people._records
            if key[:2] == (principal.tenant_id, principal.principal_id)
        }
        self._traces.erase_people_locked(principal, list(before - after))
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
