"""PostgreSQL memory, recall-trace, and knowledge repositories."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    DateTime,
    Integer,
    Text,
    bindparam,
    delete,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from agent_core.adapters.persistence.mappers import artifact_to_domain
from agent_core.adapters.persistence.sqlalchemy_models import (
    ArtifactRow,
    ConsolidationRunRow,
    ConsolidationWatermarkRow,
    KnowledgeChunkRow,
    KnowledgeDocumentRow,
    MemoryRejectionRow,
    MemoryRow,
    RecallTraceRow,
)
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.knowledge import (
    DocumentAuthority,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngestPrepared,
    KnowledgeQuery,
    KnowledgeVisibility,
    RetrievedPassage,
)
from agent_core.domain.memory import (
    SENSITIVITY_ORDER,
    BeliefRejection,
    BeliefType,
    ConsolidationRun,
    MemoryAuthority,
    MemoryEdit,
    MemoryRecord,
    MemoryStatus,
    Polarity,
    Portability,
    RecallQuery,
    RecallTrace,
    RecallTraceView,
    Sensitivity,
    TracedBelief,
    TracedPassage,
    lexical_query_terms,
)
from agent_core.domain.trajectory import ArtifactRef
from agent_core.ports.determinism import Clock

_LIVE = (MemoryStatus.ACTIVE.value, MemoryStatus.PROVISIONAL.value)


def _rowcount(result: Any) -> int:
    return int(result.rowcount or 0)


# The operator tier of a recall trace lives inside the stored JSON document, so
# its expiry is a JSONB rewrite over a bounded, index-ordered set of identifiers
# rather than a column update. `dropped_for_budget_count` keeps what the nulled
# identifier list used to say, which is all the user-safe projection needs.
_DROPPED_LENGTH = (
    "CASE WHEN jsonb_typeof({trace} -> 'dropped_for_budget') = 'array' "
    "THEN jsonb_array_length({trace} -> 'dropped_for_budget') ELSE 0 END"
)
_OPERATOR_FIELDS_PRESENT = (
    "COALESCE(trace ->> 'arm_latencies_ms', '{}') <> '{}' "
    "OR COALESCE((trace ->> 'candidates')::int, 0) <> 0 "
    f"OR {_DROPPED_LENGTH.format(trace='trace')} <> 0"
)
_EXPIRE_OPERATOR_FIELDS = f"""
UPDATE recall_traces AS expiring
SET trace = expiring.trace || jsonb_build_object(
        'arm_latencies_ms', '{{}}'::jsonb,
        'candidates', 0,
        'dropped_for_budget', '[]'::jsonb,
        'dropped_for_budget_count',
        COALESCE((expiring.trace ->> 'dropped_for_budget_count')::int, 0)
        + {_DROPPED_LENGTH.format(trace="expiring.trace")}
    )
WHERE expiring.id IN (
    SELECT id
    FROM recall_traces
    WHERE operator_fields_expire_at <= :now AND ({_OPERATOR_FIELDS_PRESENT})
    ORDER BY operator_fields_expire_at, id
    LIMIT :limit
    FOR UPDATE SKIP LOCKED
)
"""


def _memory_values(value: MemoryRecord) -> dict[str, Any]:
    data = value.model_dump(mode="python")
    for key in (
        "sensitivity",
        "status",
        "belief_type",
        "polarity",
        "portability",
        "authority",
    ):
        data[key] = data[key].value
    data["conflicts_with"] = [str(item) for item in value.conflicts_with]
    return data


def _memory(row: MemoryRow) -> MemoryRecord:
    return MemoryRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        scope=row.scope,
        subject=row.subject,
        statement=row.statement,
        source_session_id=row.source_session_id,
        source_event_ids=list(row.source_event_ids),
        confidence=row.confidence,
        sensitivity=Sensitivity(row.sensitivity),
        valid_from=row.valid_from,
        expires_at=row.expires_at,
        status=MemoryStatus(row.status),
        belief_type=BeliefType(row.belief_type),
        polarity=Polarity(row.polarity),
        portability=Portability(row.portability),
        origin_scopes=list(row.origin_scopes),
        corroboration_count=row.corroboration_count,
        last_reinforced_at=row.last_reinforced_at,
        valid_to=row.valid_to,
        superseded_by=row.superseded_by,
        conflicts_with=[UUID(item) for item in row.conflicts_with],
        flagged_for_review=row.flagged_for_review,
        formation_run_id=row.formation_run_id,
        consolidation_policy_version=row.consolidation_policy_version,
        authority=MemoryAuthority(row.authority),
        utility=row.utility,
        store_position=row.store_position,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _rejection_values(value: BeliefRejection) -> dict[str, Any]:
    data = value.model_dump(mode="python")
    data["kind"] = value.kind.value
    data["belief_type"] = value.belief_type.value
    return data


def _rejection(row: MemoryRejectionRow) -> BeliefRejection:
    return BeliefRejection.model_validate(
        {key: getattr(row, key) for key in BeliefRejection.model_fields}
    )


def _consolidation_values(value: ConsolidationRun) -> dict[str, Any]:
    return value.model_dump(mode="python")


def _consolidation(row: ConsolidationRunRow) -> ConsolidationRun:
    return ConsolidationRun.model_validate(
        {key: getattr(row, key) for key in ConsolidationRun.model_fields}
    )


def _allowed_sensitivities(ceiling: Sensitivity) -> tuple[str, ...]:
    return tuple(
        value.value
        for value in Sensitivity
        if SENSITIVITY_ORDER[value] <= SENSITIVITY_ORDER[ceiling]
    )


class PostgresMemoryStore:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    async def next_position(self) -> int:
        value = await self._session.scalar(select(func.nextval("memory_store_position_seq")))
        if value is None:
            raise RuntimeError("memory position sequence returned no value")
        return int(value)

    async def get(self, belief_id: UUID, principal: Principal) -> MemoryRecord:
        row = (
            await self._session.scalars(
                select(MemoryRow).where(
                    MemoryRow.id == belief_id,
                    MemoryRow.tenant_id == principal.tenant_id,
                    MemoryRow.principal_id == principal.principal_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("memory not found")
        return _memory(row)

    async def query(self, query: RecallQuery) -> list[MemoryRecord]:
        as_of = query.as_of or self._clock.now()
        predicates: list[Any] = [
            MemoryRow.tenant_id == query.tenant_id,
            MemoryRow.principal_id == query.principal_id,
            MemoryRow.valid_from <= as_of,
            or_(MemoryRow.valid_to.is_(None), MemoryRow.valid_to > as_of),
            or_(MemoryRow.expires_at.is_(None), MemoryRow.expires_at > as_of),
            MemoryRow.sensitivity.in_(_allowed_sensitivities(query.sensitivity_ceiling)),
        ]
        if not query.include_superseded and query.as_of is None:
            predicates.append(MemoryRow.status.in_(_LIVE))
        if query.belief_types:
            predicates.append(
                MemoryRow.belief_type.in_(tuple(item.value for item in query.belief_types))
            )
        terms = lexical_query_terms(query.text)
        if terms:
            # Any-term semantics: lexical recall is a ranking arm, so one term
            # matching is enough to make a record a candidate for the ranker.
            vector = func.to_tsvector("simple", MemoryRow.subject + " " + MemoryRow.statement)
            text_match: ColumnElement[bool] = or_(
                *[vector.op("@@")(func.plainto_tsquery("simple", term)) for term in terms]
            )
            if query.subjects:
                text_match = or_(
                    text_match,
                    func.lower(MemoryRow.subject).in_(
                        tuple(item.casefold() for item in query.subjects)
                    ),
                )
            predicates.append(text_match)
        rows = list(
            (
                await self._session.scalars(
                    select(MemoryRow)
                    .where(*predicates)
                    .order_by(MemoryRow.store_position.desc(), MemoryRow.id)
                    .limit(max(query.max_items * 8, 64))
                )
            ).all()
        )
        subjects = {item.casefold() for item in query.subjects}
        return [
            _memory(row)
            for row in rows
            if not (
                row.portability == Portability.LOCAL.value
                and row.scope != query.current_scope
                and row.subject.casefold() not in subjects
            )
        ]

    async def related(
        self,
        tenant_id: str,
        principal_id: str,
        subject: str,
        belief_type: BeliefType,
    ) -> list[MemoryRecord]:
        rows = list(
            (
                await self._session.scalars(
                    select(MemoryRow).where(
                        MemoryRow.tenant_id == tenant_id,
                        MemoryRow.principal_id == principal_id,
                        func.lower(MemoryRow.subject) == subject.casefold(),
                        MemoryRow.belief_type == belief_type.value,
                        MemoryRow.status.in_(_LIVE),
                    )
                )
            ).all()
        )
        return [_memory(row) for row in rows]

    async def upsert_belief(self, belief: MemoryRecord) -> MemoryRecord:
        statement = (
            pg_insert(MemoryRow)
            .values(**_memory_values(belief))
            .on_conflict_do_nothing(index_elements=[MemoryRow.id])
        )
        result = await self._session.execute(statement)
        if not _rowcount(result):
            existing = await self.get(
                belief.id, Principal(tenant_id=belief.tenant_id, principal_id=belief.principal_id)
            )
            if existing != belief:
                raise ConflictError("memory id identifies different content")
        return belief

    async def reinforce(self, belief: MemoryRecord) -> MemoryRecord:
        result = await self._session.execute(
            update(MemoryRow)
            .where(
                MemoryRow.id == belief.id,
                MemoryRow.tenant_id == belief.tenant_id,
                MemoryRow.principal_id == belief.principal_id,
            )
            .values(**_memory_values(belief))
        )
        if not _rowcount(result):
            raise NotFoundError("memory not found")
        return belief

    async def supersede(
        self, current: MemoryRecord, replacement: MemoryRecord
    ) -> tuple[MemoryRecord, MemoryRecord]:
        # The replacement row must exist before the retired row can point at it
        # through fk_memories_superseded_by_memories. A savepoint ensures a
        # stale-current conflict cannot leave that replacement behind when the
        # caller handles the conflict and continues the outer transaction.
        async with self._session.begin_nested():
            await self._session.execute(pg_insert(MemoryRow).values(**_memory_values(replacement)))
            result = await self._session.execute(
                update(MemoryRow)
                .where(MemoryRow.id == current.id, MemoryRow.status.in_(_LIVE))
                .values(**_memory_values(current))
            )
            if not _rowcount(result):
                raise ConflictError("memory was already inactive")
        return current, replacement

    async def list_memories(
        self,
        principal: Principal,
        *,
        include_inactive: bool = False,
        session_id: UUID | None = None,
        limit: int = 200,
    ) -> list[MemoryRecord]:
        predicates: list[Any] = [
            MemoryRow.tenant_id == principal.tenant_id,
            MemoryRow.principal_id == principal.principal_id,
        ]
        if not include_inactive:
            predicates.append(MemoryRow.status.in_(_LIVE))
        if session_id is not None:
            predicates.append(MemoryRow.source_session_id == session_id)
        rows = list(
            (
                await self._session.scalars(
                    select(MemoryRow)
                    .where(*predicates)
                    .order_by(MemoryRow.store_position.desc(), MemoryRow.id)
                    .limit(limit)
                )
            ).all()
        )
        return [_memory(row) for row in rows]

    async def list_idle(
        self,
        principal: Principal,
        *,
        reinforced_before: datetime,
        limit: int,
    ) -> list[MemoryRecord]:
        rows = list(
            (
                await self._session.scalars(
                    select(MemoryRow)
                    .where(
                        MemoryRow.tenant_id == principal.tenant_id,
                        MemoryRow.principal_id == principal.principal_id,
                        MemoryRow.status.in_(_LIVE),
                        MemoryRow.last_reinforced_at <= reinforced_before,
                    )
                    .order_by(MemoryRow.last_reinforced_at, MemoryRow.id)
                    .limit(limit)
                )
            ).all()
        )
        return [_memory(row) for row in rows]

    async def edit(
        self, belief_id: UUID, principal: Principal, edit: MemoryEdit, edited: MemoryRecord
    ) -> MemoryRecord:
        del edit
        await self.get(belief_id, principal)
        return await self.reinforce(edited)

    async def delete(
        self, belief_id: UUID, principal: Principal, tombstone: BeliefRejection
    ) -> None:
        result = await self._session.execute(
            delete(MemoryRow).where(
                MemoryRow.id == belief_id,
                MemoryRow.tenant_id == principal.tenant_id,
                MemoryRow.principal_id == principal.principal_id,
            )
        )
        if not _rowcount(result):
            raise NotFoundError("memory not found")
        await self._session.execute(
            pg_insert(MemoryRejectionRow).values(**_rejection_values(tombstone))
        )

    async def reject(self, rejection: BeliefRejection, updated: MemoryRecord) -> MemoryRecord:
        await self._session.execute(
            pg_insert(MemoryRejectionRow)
            .values(**_rejection_values(rejection))
            .on_conflict_do_update(
                index_elements=[MemoryRejectionRow.id],
                set_={"replacement_id": rejection.replacement_id},
            )
        )
        return await self.reinforce(updated)

    async def outstanding_rejections(
        self, tenant_id: str, principal_id: str
    ) -> list[BeliefRejection]:
        rows = list(
            (
                await self._session.scalars(
                    select(MemoryRejectionRow)
                    .where(
                        MemoryRejectionRow.tenant_id == tenant_id,
                        MemoryRejectionRow.principal_id == principal_id,
                    )
                    .order_by(MemoryRejectionRow.created_at, MemoryRejectionRow.id)
                )
            ).all()
        )
        return [_rejection(row) for row in rows]

    async def record_consolidation(self, run: ConsolidationRun) -> ConsolidationRun:
        await self._session.execute(
            pg_insert(ConsolidationRunRow)
            .values(**_consolidation_values(run))
            .on_conflict_do_nothing(index_elements=[ConsolidationRunRow.id])
        )
        return run

    async def list_consolidations(
        self,
        principal: Principal,
        *,
        session_id: UUID | None = None,
        limit: int = 100,
    ) -> list[ConsolidationRun]:
        predicates = [
            ConsolidationRunRow.tenant_id == principal.tenant_id,
            ConsolidationRunRow.principal_id == principal.principal_id,
        ]
        if session_id is not None:
            predicates.append(ConsolidationRunRow.session_id == session_id)
        rows = list(
            (
                await self._session.scalars(
                    select(ConsolidationRunRow)
                    .where(*predicates)
                    .order_by(
                        ConsolidationRunRow.started_at.desc(),
                        ConsolidationRunRow.id.desc(),
                    )
                    .limit(limit)
                )
            ).all()
        )
        return [_consolidation(row) for row in rows]

    async def consolidation_watermark(self, session_id: UUID, principal: Principal) -> int:
        value = await self._session.scalar(
            select(ConsolidationWatermarkRow.sequence).where(
                ConsolidationWatermarkRow.tenant_id == principal.tenant_id,
                ConsolidationWatermarkRow.principal_id == principal.principal_id,
                ConsolidationWatermarkRow.session_id == session_id,
            )
        )
        return 0 if value is None else int(value)

    async def set_consolidation_watermark(
        self, session_id: UUID, principal: Principal, sequence: int
    ) -> None:
        statement = pg_insert(ConsolidationWatermarkRow).values(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            session_id=session_id,
            sequence=sequence,
            updated_at=self._clock.now(),
        )
        statement = statement.on_conflict_do_update(
            index_elements=[
                ConsolidationWatermarkRow.tenant_id,
                ConsolidationWatermarkRow.principal_id,
                ConsolidationWatermarkRow.session_id,
            ],
            set_={
                "sequence": func.greatest(
                    ConsolidationWatermarkRow.sequence, statement.excluded.sequence
                ),
                "updated_at": statement.excluded.updated_at,
            },
        )
        await self._session.execute(statement)

    async def expire(self, principal: Principal) -> list[MemoryRecord]:
        now = self._clock.now()
        rows = list(
            (
                await self._session.scalars(
                    select(MemoryRow).where(
                        MemoryRow.tenant_id == principal.tenant_id,
                        MemoryRow.principal_id == principal.principal_id,
                        MemoryRow.status.in_(_LIVE),
                        MemoryRow.expires_at.is_not(None),
                        MemoryRow.expires_at <= now,
                    )
                )
            ).all()
        )
        expired: list[MemoryRecord] = []
        for row in rows:
            value = _memory(row).model_copy(
                update={
                    "status": MemoryStatus.EXPIRED,
                    "valid_to": now,
                    "store_position": await self.next_position(),
                    "updated_at": now,
                }
            )
            await self.reinforce(value)
            expired.append(value)
        return expired


class PostgresTraceStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, trace: RecallTrace) -> None:
        statement = (
            pg_insert(RecallTraceRow)
            .values(
                id=trace.id,
                tenant_id=trace.tenant_id,
                principal_id=trace.principal_id,
                session_id=trace.session_id,
                turn_id=trace.turn_id,
                trace=trace.model_dump(mode="json"),
                created_at=trace.created_at,
                operator_fields_expire_at=trace.operator_fields_expire_at,
            )
            .on_conflict_do_nothing(index_elements=[RecallTraceRow.id])
        )
        result = await self._session.execute(statement)
        if not _rowcount(result):
            row = await self._session.get(RecallTraceRow, trace.id)
            if row is None or RecallTrace.model_validate(row.trace) != trace:
                raise ConflictError("trace id identifies different content")

    async def for_turn(self, turn_id: UUID) -> list[RecallTrace]:
        rows = list(
            (
                await self._session.scalars(
                    select(RecallTraceRow)
                    .where(RecallTraceRow.turn_id == turn_id)
                    .order_by(RecallTraceRow.created_at, RecallTraceRow.id)
                )
            ).all()
        )
        return [RecallTrace.model_validate(row.trace) for row in rows]

    async def get(self, trace_id: UUID, principal: Principal) -> RecallTrace:
        row = (
            await self._session.scalars(
                select(RecallTraceRow).where(
                    RecallTraceRow.id == trace_id,
                    RecallTraceRow.tenant_id == principal.tenant_id,
                    RecallTraceRow.principal_id == principal.principal_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("recall trace not found")
        return RecallTrace.model_validate(row.trace)

    async def mark_cited(
        self, trace_id: UUID, principal: Principal, cited: Sequence[UUID]
    ) -> RecallTrace:
        # The row is locked before the document is read, so two completions
        # racing on the same trace union their citations instead of one
        # rewriting the JSONB the other had already widened.
        row = (
            await self._session.scalars(
                select(RecallTraceRow)
                .where(
                    RecallTraceRow.id == trace_id,
                    RecallTraceRow.tenant_id == principal.tenant_id,
                    RecallTraceRow.principal_id == principal.principal_id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("recall trace not found")
        trace = RecallTrace.model_validate(row.trace)
        marked = list(trace.cited)
        known = set(marked)
        for belief_id in cited:
            if belief_id not in known:
                known.add(belief_id)
                marked.append(belief_id)
        if marked == trace.cited:
            return trace
        updated = trace.model_copy(update={"cited": marked})
        row.trace = updated.model_dump(mode="json")
        return updated

    async def expire_operator_fields(self, now: datetime, limit: int) -> int:
        if limit < 0:
            raise ValueError("recall-trace expiry limit must be nonnegative")
        statement = text(_EXPIRE_OPERATOR_FIELDS).bindparams(
            bindparam("now", value=now, type_=DateTime(timezone=True)),
            bindparam("limit", value=limit, type_=Integer()),
        )
        return _rowcount(await self._session.execute(statement))

    async def user_view(
        self, turn_id: UUID, viewing_surface_id: str, viewing_ceiling: str
    ) -> RecallTraceView:
        del viewing_surface_id
        traces = await self.for_turn(turn_id)
        ceiling = Sensitivity(viewing_ceiling)
        beliefs: list[TracedBelief] = []
        passages: list[TracedPassage] = []
        for trace in traces:
            effective = min(
                SENSITIVITY_ORDER[trace.sensitivity_ceiling], SENSITIVITY_ORDER[ceiling]
            )
            beliefs.extend(
                TracedBelief(
                    belief_id=item.belief_id,
                    subject=item.subject,
                    statement=item.statement,
                    learned_at=item.valid_from,
                    origin_scope=item.origin_scope,
                    carried=item.carried,
                    authority=item.authority,
                    source_event_id=item.source_event_ids[0] if item.source_event_ids else None,
                    confidence_band=item.confidence_band,
                    used=item.belief_id in trace.cited,
                )
                for item in trace.beliefs
                if SENSITIVITY_ORDER[item.sensitivity] <= effective and not item.blocked
            )
            passages.extend(
                item for item in trace.passages if SENSITIVITY_ORDER[item.sensitivity] <= effective
            )
        as_of = max(
            (trace.created_at for trace in traces),
            default=datetime.min.replace(tzinfo=UTC),
        )
        return RecallTraceView(
            turn_id=turn_id,
            moments=[trace.moment for trace in traces],
            beliefs=beliefs,
            passages=passages,
            considered_not_shown=sum(trace.considered_not_shown for trace in traces),
            withheld_by_safety=sum(len(trace.blocked) for trace in traces),
            as_of=as_of,
        )

    async def mark_document_deleted(self, tenant_id: str, document_id: UUID) -> None:
        rows = list(
            (
                await self._session.scalars(
                    select(RecallTraceRow).where(
                        RecallTraceRow.tenant_id == tenant_id,
                        RecallTraceRow.trace.contains(
                            {"passages": [{"document_id": str(document_id)}]}
                        ),
                    )
                )
            ).all()
        )
        for row in rows:
            trace = RecallTrace.model_validate(row.trace)
            passages = [
                item.model_copy(update={"text": None, "deleted": True})
                if item.document_id == document_id
                else item
                for item in trace.passages
            ]
            if passages != trace.passages:
                updated = trace.model_copy(update={"passages": passages})
                row.trace = updated.model_dump(mode="json")


def _knowledge_document(row: KnowledgeDocumentRow, source: ArtifactRow) -> KnowledgeDocument:
    return KnowledgeDocument(
        row_id=row.row_id,
        document_id=row.document_id,
        tenant_id=row.tenant_id,
        ingested_by_principal_id=row.ingested_by_principal_id,
        visibility=KnowledgeVisibility(row.visibility),
        project_scope=row.project_scope,
        title=row.title,
        source_ref=artifact_to_domain(source),
        media_type=row.media_type,
        doc_date=row.doc_date,
        authority=DocumentAuthority(row.authority),
        version=row.version,
        chunker_version=row.chunker_version,
        superseded_by=row.superseded_by,
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        ingested_at=row.ingested_at,
        sensitivity=Sensitivity(row.sensitivity),
    )


def _chunk(row: KnowledgeChunkRow) -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=row.chunk_id,
        document_row_id=row.document_row_id,
        document_id=row.document_id,
        version=row.version,
        ordinal=row.ordinal,
        heading_path=list(row.heading_path),
        text=row.text,
        tokens=row.tokens,
        contains_instruction_like_text=row.contains_instruction_like_text,
        content_sha256=row.content_sha256,
    )


class PostgresKnowledgeStore:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    async def ingest(self, prepared: KnowledgeIngestPrepared) -> None:
        document = prepared.document
        await self._session.execute(
            update(KnowledgeDocumentRow)
            .where(
                KnowledgeDocumentRow.tenant_id == document.tenant_id,
                KnowledgeDocumentRow.document_id == document.document_id,
                KnowledgeDocumentRow.valid_to.is_(None),
            )
            .values(superseded_by=document.row_id, valid_to=document.valid_from)
        )
        values = document.model_dump(mode="python", exclude={"source_ref"})
        values["source_artifact_id"] = document.source_ref.id
        for key in ("visibility", "authority", "sensitivity"):
            values[key] = values[key].value
        await self._session.execute(pg_insert(KnowledgeDocumentRow).values(**values))
        await self._session.execute(
            pg_insert(KnowledgeChunkRow),
            [chunk.model_dump(mode="python") for chunk in prepared.chunks],
        )

    async def latest(self, tenant_id: str, document_id: UUID) -> KnowledgeDocument | None:
        result = (
            await self._session.execute(
                select(KnowledgeDocumentRow, ArtifactRow)
                .join(ArtifactRow, ArtifactRow.id == KnowledgeDocumentRow.source_artifact_id)
                .where(
                    KnowledgeDocumentRow.tenant_id == tenant_id,
                    KnowledgeDocumentRow.document_id == document_id,
                )
                .order_by(KnowledgeDocumentRow.version.desc())
                .limit(1)
                .with_for_update(of=KnowledgeDocumentRow)
            )
        ).one_or_none()
        return None if result is None else _knowledge_document(result[0], result[1])

    async def search(self, query: KnowledgeQuery) -> list[RetrievedPassage]:
        as_of = query.as_of or self._clock.now()
        vector = func.to_tsvector(
            "simple",
            func.concat(KnowledgeChunkRow.heading_path.cast(Text), " ", KnowledgeChunkRow.text),
        )
        rank = func.ts_rank_cd(vector, func.plainto_tsquery("simple", query.text))
        visibility = or_(
            KnowledgeDocumentRow.visibility == KnowledgeVisibility.TENANT.value,
            (
                (KnowledgeDocumentRow.visibility == KnowledgeVisibility.PRINCIPAL.value)
                & (KnowledgeDocumentRow.ingested_by_principal_id == query.principal_id)
            ),
            (
                (KnowledgeDocumentRow.visibility == KnowledgeVisibility.PROJECT.value)
                & (KnowledgeDocumentRow.project_scope == query.current_scope)
            ),
        )
        rows = list(
            (
                await self._session.execute(
                    select(KnowledgeChunkRow, KnowledgeDocumentRow, rank.label("rank"))
                    .join(
                        KnowledgeDocumentRow,
                        KnowledgeDocumentRow.row_id == KnowledgeChunkRow.document_row_id,
                    )
                    .where(
                        KnowledgeDocumentRow.tenant_id == query.tenant_id,
                        KnowledgeDocumentRow.valid_from <= as_of,
                        or_(
                            KnowledgeDocumentRow.valid_to.is_(None),
                            KnowledgeDocumentRow.valid_to > as_of,
                        ),
                        KnowledgeDocumentRow.sensitivity.in_(
                            _allowed_sensitivities(query.sensitivity_ceiling)
                        ),
                        visibility,
                        vector.op("@@")(func.plainto_tsquery("simple", query.text)),
                    )
                    .order_by(rank.desc(), KnowledgeChunkRow.chunk_id)
                    .limit(max(query.max_passages * 8, 64))
                )
            ).all()
        )
        per_document: defaultdict[UUID, int] = defaultdict(int)
        passages: list[RetrievedPassage] = []
        for chunk, document, raw_rank in rows:
            if per_document[document.document_id] >= query.max_per_document:
                continue
            score = min(1.0, float(raw_rank) + 0.25)
            if score < query.min_score:
                continue
            per_document[document.document_id] += 1
            passages.append(
                RetrievedPassage(
                    chunk_id=chunk.chunk_id,
                    document_id=document.document_id,
                    title=document.title,
                    heading_path=list(chunk.heading_path),
                    text=chunk.text,
                    doc_date=document.doc_date,
                    authority=DocumentAuthority(document.authority),
                    sensitivity=Sensitivity(document.sensitivity),
                    score=score,
                    arms=["lexical"],
                    instruction_like=chunk.contains_instruction_like_text,
                )
            )
        return passages

    async def get_chunk(self, chunk_id: str) -> KnowledgeChunk | None:
        row = await self._session.get(KnowledgeChunkRow, chunk_id)
        return None if row is None else _chunk(row)

    async def delete(self, document_id: UUID, principal: Principal) -> list[ArtifactRef]:
        rows = list(
            (
                await self._session.execute(
                    select(KnowledgeDocumentRow, ArtifactRow)
                    .join(ArtifactRow, ArtifactRow.id == KnowledgeDocumentRow.source_artifact_id)
                    .where(
                        KnowledgeDocumentRow.tenant_id == principal.tenant_id,
                        KnowledgeDocumentRow.ingested_by_principal_id == principal.principal_id,
                        KnowledgeDocumentRow.document_id == document_id,
                    )
                )
            ).all()
        )
        if not rows:
            raise NotFoundError("knowledge document not found")
        await self._session.execute(
            delete(KnowledgeDocumentRow).where(
                KnowledgeDocumentRow.tenant_id == principal.tenant_id,
                KnowledgeDocumentRow.ingested_by_principal_id == principal.principal_id,
                KnowledgeDocumentRow.document_id == document_id,
            )
        )
        return [artifact_to_domain(source) for _, source in rows]
