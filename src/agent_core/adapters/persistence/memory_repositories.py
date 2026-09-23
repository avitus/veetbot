"""PostgreSQL memory, recall-trace, and knowledge repositories."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import (
    DateTime,
    Integer,
    Text,
    and_,
    any_,
    bindparam,
    delete,
    false,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from agent_core.adapters.persistence.integrity import constraint_name
from agent_core.adapters.persistence.mappers import artifact_to_domain
from agent_core.adapters.persistence.people_erasure import lock_run_erasure
from agent_core.adapters.persistence.sqlalchemy_models import (
    ArtifactRow,
    ConsolidationRunRow,
    ConsolidationWatermarkRow,
    IntegratedEpisodeRow,
    KnowledgeChunkRow,
    KnowledgeDocumentRow,
    MemoryRejectionRow,
    MemoryRevisionRow,
    MemoryRow,
    RecallTraceRow,
    RunRow,
)
from agent_core.domain.agents import Principal
from agent_core.domain.erasure import erased_rejection, memory_erasure_tombstone
from agent_core.domain.errors import ConflictError, NotFoundError, RunCancelledError
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
    LIFECYCLE_POLICY_VERSION,
    SENSITIVITY_ORDER,
    BeliefRejection,
    BeliefType,
    ConsolidationRun,
    IntegratedEpisode,
    MemoryAuthority,
    MemoryBrowseQuery,
    MemoryClaimKind,
    MemoryDerivation,
    MemoryEdit,
    MemoryLongevity,
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
    TracedPersonContext,
    lexical_query_terms,
    recall_query_terms,
)
from agent_core.domain.trajectory import ArtifactRef
from agent_core.ports.determinism import Clock
from agent_core.ports.people import PeopleStore

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
        "claim_kind",
        "derivation",
        "longevity",
    ):
        data[key] = data[key].value
    data["conflicts_with"] = [str(item) for item in value.conflicts_with]
    return data


def _episode(row: IntegratedEpisodeRow) -> IntegratedEpisode:
    return IntegratedEpisode(
        id=row.id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        session_id=row.session_id,
        source_event_ids=list(row.source_event_ids),
        source_started_at=row.source_started_at,
        source_ended_at=row.source_ended_at,
        narrative=row.narrative,
        subjects=list(row.subjects),
        integration_policy_version=cast(
            Literal["episode-integration@1"], row.integration_policy_version
        ),
        derivation_key=row.derivation_key,
        created_at=row.created_at,
    )


class PostgresIntegratedEpisodeStore:
    """Owner-scoped PostgreSQL repository for integrated episodes."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def put(self, episode: IntegratedEpisode) -> IntegratedEpisode:
        statement = (
            pg_insert(IntegratedEpisodeRow)
            .values(**episode.model_dump(mode="python"))
            .on_conflict_do_nothing(
                index_elements=[
                    IntegratedEpisodeRow.tenant_id,
                    IntegratedEpisodeRow.principal_id,
                    IntegratedEpisodeRow.derivation_key,
                ]
            )
            .returning(IntegratedEpisodeRow)
        )
        try:
            async with self._session.begin_nested():
                inserted = (await self._session.scalars(statement)).one_or_none()
        except IntegrityError as exc:
            if constraint_name(exc) == "pk_integrated_episodes":
                raise ConflictError("episode id identifies different content") from exc
            raise
        if inserted is not None:
            return _episode(inserted)
        existing = (
            await self._session.scalars(
                select(IntegratedEpisodeRow).where(
                    IntegratedEpisodeRow.tenant_id == episode.tenant_id,
                    IntegratedEpisodeRow.principal_id == episode.principal_id,
                    IntegratedEpisodeRow.derivation_key == episode.derivation_key,
                )
            )
        ).one()
        if existing.erasure_pending:
            raise ConflictError("integrated episode is fenced for erasure")
        value = _episode(existing)
        if value.model_dump(exclude={"id", "created_at"}) != episode.model_dump(
            exclude={"id", "created_at"}
        ):
            raise ConflictError("episode derivation identifies different content")
        return value

    async def get(self, episode_id: UUID, principal: Principal) -> IntegratedEpisode:
        row = (
            await self._session.scalars(
                select(IntegratedEpisodeRow).where(
                    IntegratedEpisodeRow.id == episode_id,
                    IntegratedEpisodeRow.tenant_id == principal.tenant_id,
                    IntegratedEpisodeRow.principal_id == principal.principal_id,
                    ~IntegratedEpisodeRow.erasure_pending,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("integrated episode not found")
        return _episode(row)

    async def get_by_derivation(
        self, derivation_key: str, principal: Principal
    ) -> IntegratedEpisode | None:
        row = (
            await self._session.scalars(
                select(IntegratedEpisodeRow).where(
                    IntegratedEpisodeRow.derivation_key == derivation_key,
                    IntegratedEpisodeRow.tenant_id == principal.tenant_id,
                    IntegratedEpisodeRow.principal_id == principal.principal_id,
                    ~IntegratedEpisodeRow.erasure_pending,
                )
            )
        ).one_or_none()
        return None if row is None else _episode(row)

    async def for_session(
        self,
        session_id: UUID,
        principal: Principal,
        *,
        limit: int = 100,
    ) -> list[IntegratedEpisode]:
        if limit < 1:
            raise ValueError("episode page limit must be positive")
        rows = list(
            (
                await self._session.scalars(
                    select(IntegratedEpisodeRow)
                    .where(
                        IntegratedEpisodeRow.session_id == session_id,
                        IntegratedEpisodeRow.tenant_id == principal.tenant_id,
                        IntegratedEpisodeRow.principal_id == principal.principal_id,
                        ~IntegratedEpisodeRow.erasure_pending,
                    )
                    .order_by(
                        IntegratedEpisodeRow.source_started_at,
                        IntegratedEpisodeRow.id,
                    )
                    .limit(limit)
                )
            ).all()
        )
        return [_episode(row) for row in rows]

    async def delete_for_session(self, session_id: UUID, principal: Principal) -> int:
        result = await self._session.execute(
            delete(IntegratedEpisodeRow).where(
                IntegratedEpisodeRow.session_id == session_id,
                IntegratedEpisodeRow.tenant_id == principal.tenant_id,
                IntegratedEpisodeRow.principal_id == principal.principal_id,
            )
        )
        return _rowcount(result)

    async def delete_for_principal(self, principal: Principal) -> int:
        result = await self._session.execute(
            delete(IntegratedEpisodeRow).where(
                IntegratedEpisodeRow.tenant_id == principal.tenant_id,
                IntegratedEpisodeRow.principal_id == principal.principal_id,
            )
        )
        return _rowcount(result)

    async def fence_for_erasure(self, principal: Principal, session_ids: Sequence[UUID]) -> int:
        result = await self._session.execute(
            update(IntegratedEpisodeRow)
            .where(
                IntegratedEpisodeRow.tenant_id == principal.tenant_id,
                IntegratedEpisodeRow.principal_id == principal.principal_id,
                ~IntegratedEpisodeRow.erasure_pending,
                IntegratedEpisodeRow.session_id
                == any_(bindparam(None, list(session_ids), type_=ARRAY(PGUUID(as_uuid=True)))),
            )
            .values(erasure_pending=True)
        )
        return _rowcount(result)

    async def purge_erased(self, principal: Principal) -> bool:
        rows = select(IntegratedEpisodeRow.id).where(
            IntegratedEpisodeRow.tenant_id == principal.tenant_id,
            IntegratedEpisodeRow.principal_id == principal.principal_id,
            IntegratedEpisodeRow.erasure_pending,
        )
        await self._session.execute(
            delete(IntegratedEpisodeRow).where(
                IntegratedEpisodeRow.id.in_(rows.order_by(IntegratedEpisodeRow.id).limit(256)),
            )
        )
        return bool(await self._session.scalar(select(rows.exists())))


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
        claim_kind=MemoryClaimKind(row.claim_kind),
        derivation=MemoryDerivation(row.derivation),
        longevity=MemoryLongevity(row.longevity),
        last_evidence_at=row.last_evidence_at,
        last_used_at=row.last_used_at,
        evidence_count=row.evidence_count,
        lifecycle_policy_version=row.lifecycle_policy_version,
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

    async def _remember_revision(self, record: MemoryRecord) -> None:
        await self._session.execute(
            pg_insert(MemoryRevisionRow).values(
                tenant_id=record.tenant_id,
                principal_id=record.principal_id,
                belief_id=record.id,
                recorded_at=self._clock.now(),
                payload=record.model_dump(mode="json"),
            )
        )

    async def get_at(
        self, belief_id: UUID, principal: Principal, *, known_at: datetime
    ) -> MemoryRecord:
        current = await self.get(belief_id, principal)
        row = await self._session.scalar(
            select(MemoryRevisionRow)
            .where(
                MemoryRevisionRow.tenant_id == principal.tenant_id,
                MemoryRevisionRow.principal_id == principal.principal_id,
                MemoryRevisionRow.belief_id == belief_id,
                MemoryRevisionRow.recorded_at <= known_at,
            )
            .order_by(MemoryRevisionRow.recorded_at.desc(), MemoryRevisionRow.id.desc())
            .limit(1)
        )
        if row is None:
            raise NotFoundError("memory was not recorded at this time")
        historical = MemoryRecord.model_validate(row.payload)
        return historical.model_copy(
            update={
                "sensitivity": max(
                    (current.sensitivity, historical.sensitivity), key=SENSITIVITY_ORDER.__getitem__
                )
            }
        )

    async def next_position(self) -> int:
        value = await self._session.scalar(select(func.nextval("memory_store_position_seq")))
        if value is None:
            raise RuntimeError("memory position sequence returned no value")
        return int(value)

    async def head_position(self, principal: Principal) -> int:
        # The principal's own newest write, not the sequence's last value: the
        # sequence is cluster-wide and its counter is not transactional, so it
        # would report positions this principal cannot see.
        value = await self._session.scalar(
            select(func.max(MemoryRow.store_position)).where(
                MemoryRow.tenant_id == principal.tenant_id,
                MemoryRow.principal_id == principal.principal_id,
                ~MemoryRow.erasure_pending,
            )
        )
        return 0 if value is None else int(value)

    async def get(self, belief_id: UUID, principal: Principal) -> MemoryRecord:
        row = (
            await self._session.scalars(
                select(MemoryRow).where(
                    MemoryRow.id == belief_id,
                    MemoryRow.tenant_id == principal.tenant_id,
                    MemoryRow.principal_id == principal.principal_id,
                    ~MemoryRow.erasure_pending,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("memory not found")
        return _memory(row)

    async def query(self, query: RecallQuery) -> list[MemoryRecord]:
        if query.known_at is not None:
            return await self._query_at(query)
        as_of = query.as_of or self._clock.now()
        predicates: list[Any] = [
            MemoryRow.tenant_id == query.tenant_id,
            MemoryRow.principal_id == query.principal_id,
            ~MemoryRow.erasure_pending,
            MemoryRow.valid_from <= as_of,
            or_(MemoryRow.valid_to.is_(None), MemoryRow.valid_to > as_of),
            or_(MemoryRow.expires_at.is_(None), MemoryRow.expires_at > as_of),
            MemoryRow.sensitivity.in_(_allowed_sensitivities(query.sensitivity_ceiling)),
        ]
        if query.include_ids is not None:
            predicates.append(MemoryRow.id.in_(query.include_ids))
        if query.min_store_position:
            predicates.append(MemoryRow.store_position > query.min_store_position)
        if not query.include_superseded and query.as_of is None:
            predicates.append(MemoryRow.status.in_(_LIVE))
        if not query.include_provisional:
            predicates.append(MemoryRow.status != MemoryStatus.PROVISIONAL.value)
        if query.belief_types:
            predicates.append(
                MemoryRow.belief_type.in_(tuple(item.value for item in query.belief_types))
            )
        predicates.append(
            or_(
                MemoryRow.portability != Portability.LOCAL.value,
                MemoryRow.scope == query.current_scope,
                func.lower(MemoryRow.subject).in_(
                    tuple(item.casefold() for item in query.subjects)
                ),
            )
        )
        terms = recall_query_terms(query.text)
        if query.include_ids is None and (
            query.text is not None or query.subjects or query.structured_belief_types
        ):
            # Any-term semantics: lexical recall is a ranking arm, so one term
            # matching is enough to make a record a candidate for the ranker.
            vector = func.to_tsvector("simple", MemoryRow.subject + " " + MemoryRow.statement)
            text_match: ColumnElement[bool] = or_(
                false(), *[vector.op("@@")(func.plainto_tsquery("simple", term)) for term in terms]
            )
            if query.subjects:
                text_match = or_(
                    text_match,
                    func.lower(MemoryRow.subject).in_(
                        tuple(item.casefold() for item in query.subjects)
                    ),
                )
            if query.structured_belief_types:
                text_match = or_(
                    text_match,
                    MemoryRow.belief_type.in_(
                        tuple(item.value for item in query.structured_belief_types)
                    ),
                )
            predicates.append(
                or_(text_match, MemoryRow.id.in_(query.expand_ids))
                if query.expand_ids
                else text_match
            )
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
        return [_memory(row) for row in rows]

    async def _query_at(self, query: RecallQuery) -> list[MemoryRecord]:
        """Select the original revision before applying relevance or result limits."""
        latest = (
            select(MemoryRevisionRow)
            .where(
                MemoryRevisionRow.tenant_id == query.tenant_id,
                MemoryRevisionRow.principal_id == query.principal_id,
                MemoryRevisionRow.recorded_at <= query.known_at,
            )
            .distinct(MemoryRevisionRow.belief_id)
            .order_by(
                MemoryRevisionRow.belief_id,
                MemoryRevisionRow.recorded_at.desc(),
                MemoryRevisionRow.id.desc(),
            )
            .subquery()
        )
        payload = latest.c.payload
        at = query.as_of or self._clock.now()
        allowed = _allowed_sensitivities(query.sensitivity_ceiling)
        subjects = tuple(item.casefold() for item in query.subjects)
        predicates = [
            MemoryRow.tenant_id == query.tenant_id,
            MemoryRow.principal_id == query.principal_id,
            ~MemoryRow.erasure_pending,
            MemoryRow.sensitivity.in_(allowed),
            payload["sensitivity"].astext.in_(allowed),
            sql_cast(payload["valid_from"].astext, DateTime(timezone=True)) <= at,
            or_(
                payload["valid_to"].astext.is_(None),
                sql_cast(payload["valid_to"].astext, DateTime(timezone=True)) > at,
            ),
            or_(
                payload["expires_at"].astext.is_(None),
                sql_cast(payload["expires_at"].astext, DateTime(timezone=True)) > at,
            ),
            sql_cast(payload["store_position"].astext, Integer) > query.min_store_position,
            or_(
                MemoryRow.portability != Portability.LOCAL.value,
                MemoryRow.scope == query.current_scope,
                func.lower(MemoryRow.subject).in_(subjects),
            ),
            or_(
                payload["portability"].astext != Portability.LOCAL.value,
                payload["scope"].astext == query.current_scope,
                func.lower(payload["subject"].astext).in_(subjects),
            ),
        ]
        if query.include_ids is not None:
            predicates.append(latest.c.belief_id.in_(query.include_ids))
        if not query.include_superseded and query.as_of is None:
            predicates.append(payload["status"].astext.in_(_LIVE))
        if query.belief_types:
            predicates.append(
                payload["belief_type"].astext.in_(tuple(kind.value for kind in query.belief_types))
            )
        if not query.include_provisional:
            predicates.append(payload["status"].astext != MemoryStatus.PROVISIONAL.value)
        terms = recall_query_terms(query.text)
        if query.include_ids is None and (
            query.text is not None or query.subjects or query.structured_belief_types
        ):
            vector = func.to_tsvector(
                "simple", payload["subject"].astext + " " + payload["statement"].astext
            )
            predicates.append(
                or_(
                    false(),
                    *[vector.op("@@")(func.plainto_tsquery("simple", term)) for term in terms],
                    payload["belief_type"].astext.in_(
                        tuple(kind.value for kind in query.structured_belief_types)
                    ),
                    func.lower(payload["subject"].astext).in_(subjects),
                    latest.c.belief_id.in_(query.expand_ids),
                )
            )
        rows = await self._session.execute(
            select(payload, MemoryRow.sensitivity)
            .join(
                MemoryRow,
                and_(
                    MemoryRow.id == latest.c.belief_id,
                    MemoryRow.tenant_id == latest.c.tenant_id,
                    MemoryRow.principal_id == latest.c.principal_id,
                ),
            )
            .where(*predicates)
            .order_by(
                sql_cast(payload["store_position"].astext, Integer).desc(), latest.c.belief_id
            )
            .limit(max(query.max_items * 8, 64))
        )
        result = []
        for data, sensitivity in rows:
            record = MemoryRecord.model_validate(data)
            result.append(
                record.model_copy(
                    update={
                        "sensitivity": max(
                            (record.sensitivity, Sensitivity(sensitivity)),
                            key=SENSITIVITY_ORDER.__getitem__,
                        )
                    }
                )
            )
        return result

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
                        ~MemoryRow.erasure_pending,
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
        else:
            await self._remember_revision(belief)
        return belief

    async def reinforce(self, belief: MemoryRecord) -> MemoryRecord:
        result = await self._session.execute(
            update(MemoryRow)
            .where(
                MemoryRow.id == belief.id,
                MemoryRow.tenant_id == belief.tenant_id,
                MemoryRow.principal_id == belief.principal_id,
                ~MemoryRow.erasure_pending,
            )
            .values(**_memory_values(belief))
        )
        if not _rowcount(result):
            raise NotFoundError("memory not found")
        await self._remember_revision(belief)
        return belief

    async def supersede(
        self, current: MemoryRecord, replacement: MemoryRecord
    ) -> tuple[MemoryRecord, MemoryRecord]:
        # The replacement row must exist before the retired row can point at it
        # through fk_memories_superseded_by_memories. A savepoint ensures a
        # stale-current conflict cannot leave that replacement behind when the
        # caller handles the conflict and continues the outer transaction.
        async with self._session.begin_nested():
            if await self._session.scalar(
                select(MemoryRow.id).where(MemoryRow.id == replacement.id)
            ):
                raise ConflictError("replacement memory already exists")
            await self._session.execute(pg_insert(MemoryRow).values(**_memory_values(replacement)))
            result = await self._session.execute(
                update(MemoryRow)
                .where(
                    MemoryRow.id == current.id,
                    MemoryRow.status.in_(_LIVE),
                    ~MemoryRow.erasure_pending,
                )
                .values(**_memory_values(current))
            )
            if not _rowcount(result):
                raise ConflictError("memory was already inactive")
            await self._remember_revision(current)
            await self._remember_revision(replacement)
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
            ~MemoryRow.erasure_pending,
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

    async def browse(self, query: MemoryBrowseQuery) -> list[MemoryRecord]:
        predicates: list[Any] = [
            MemoryRow.tenant_id == query.tenant_id,
            MemoryRow.principal_id == query.principal_id,
            ~MemoryRow.erasure_pending,
            MemoryRow.sensitivity.in_(_allowed_sensitivities(query.ceiling)),
            MemoryRow.status.in_(tuple(status.value for status in query.statuses)),
        ]
        if query.belief_types:
            predicates.append(
                MemoryRow.belief_type.in_(tuple(item.value for item in query.belief_types))
            )
        if query.subject is not None:
            # Both sides lowercase: SQL `lower()` on the column and Python
            # `lower()` on the term. `casefold()` here would disagree with
            # the column for non-ASCII subjects and break adapter parity.
            predicates.append(func.lower(MemoryRow.subject) == query.subject.lower())
        if query.session_id is not None:
            predicates.append(MemoryRow.source_session_id == query.session_id)
        if query.flagged_for_review is not None:
            predicates.append(MemoryRow.flagged_for_review.is_(query.flagged_for_review))
        terms = lexical_query_terms(query.text)
        if terms:
            # Any-term semantics, matching `query()`: a belief matches when it
            # overlaps one term or more.
            vector = func.to_tsvector("simple", MemoryRow.subject + " " + MemoryRow.statement)
            predicates.append(
                or_(*[vector.op("@@")(func.plainto_tsquery("simple", term)) for term in terms])
            )
        if query.cursor is not None:
            cursor_position, cursor_id = query.cursor
            predicates.append(
                or_(
                    MemoryRow.store_position < cursor_position,
                    and_(MemoryRow.store_position == cursor_position, MemoryRow.id > cursor_id),
                )
            )
        rows = list(
            (
                await self._session.scalars(
                    select(MemoryRow)
                    .where(*predicates)
                    .order_by(MemoryRow.store_position.desc(), MemoryRow.id)
                    .limit(query.limit + 1)
                )
            ).all()
        )
        return [_memory(row) for row in rows]

    async def list_idle(
        self,
        principal: Principal,
        *,
        evidence_before: datetime,
        decay_confidence_ceiling: float | None = None,
        limit: int,
    ) -> list[MemoryRecord]:
        predicates: list[ColumnElement[bool]] = [
            MemoryRow.tenant_id == principal.tenant_id,
            MemoryRow.principal_id == principal.principal_id,
            ~MemoryRow.erasure_pending,
            MemoryRow.status.in_(_LIVE),
            MemoryRow.last_evidence_at <= evidence_before,
        ]
        if decay_confidence_ceiling is not None:
            predicates.append(MemoryRow.lifecycle_policy_version != "people-lifecycle@1")
            predicates.append(
                or_(
                    MemoryRow.status == MemoryStatus.PROVISIONAL.value,
                    MemoryRow.confidence < decay_confidence_ceiling,
                )
            )
        rows = list(
            (
                await self._session.scalars(
                    select(MemoryRow)
                    .where(*predicates)
                    .order_by(MemoryRow.last_evidence_at, MemoryRow.id)
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

    async def _erase_rejections(self, principal: Principal, belief_ids: Sequence[UUID]) -> None:
        keys = bindparam(None, list(belief_ids), type_=ARRAY(PGUUID(as_uuid=True)))
        await self._session.execute(
            update(MemoryRejectionRow)
            .where(
                MemoryRejectionRow.tenant_id == principal.tenant_id,
                MemoryRejectionRow.principal_id == principal.principal_id,
                or_(
                    MemoryRejectionRow.belief_id == any_(keys),
                    MemoryRejectionRow.replacement_id == any_(keys),
                ),
            )
            .values(
                kind="deleted",
                subject="erased memory",
                statement=None,
                replacement_id=None,
                trace_id=None,
            )
        )

    async def fence_for_erasure(self, principal: Principal, belief_ids: Sequence[UUID]) -> int:
        result = await self._session.execute(
            update(MemoryRow)
            .where(
                MemoryRow.tenant_id == principal.tenant_id,
                MemoryRow.principal_id == principal.principal_id,
                MemoryRow.id
                == any_(bindparam(None, list(belief_ids), type_=ARRAY(PGUUID(as_uuid=True)))),
                ~MemoryRow.erasure_pending,
            )
            .values(erasure_pending=True)
        )
        await self._erase_rejections(principal, belief_ids)
        return _rowcount(result)

    async def purge_erased(
        self, principal: Principal, belief_ids: Sequence[UUID], *, operation_id: UUID
    ) -> int:
        if len(belief_ids) > 256:
            raise ValueError("memory erasure batches contain at most 256 beliefs")
        rows = list(
            (
                await self._session.scalars(
                    select(MemoryRow)
                    .where(
                        MemoryRow.tenant_id == principal.tenant_id,
                        MemoryRow.principal_id == principal.principal_id,
                        MemoryRow.id.in_(belief_ids),
                        MemoryRow.erasure_pending,
                    )
                    .with_for_update()
                )
            ).all()
        )
        if not rows:
            return 0
        await self._session.execute(
            pg_insert(MemoryRejectionRow),
            [
                _rejection_values(
                    memory_erasure_tombstone(_memory(row), operation_id, self._clock.now())
                )
                for row in rows
            ],
        )
        await self._session.execute(
            delete(MemoryRow).where(
                MemoryRow.tenant_id == principal.tenant_id,
                MemoryRow.principal_id == principal.principal_id,
                MemoryRow.id.in_([row.id for row in rows]),
                MemoryRow.erasure_pending,
            )
        )
        return len(rows)

    async def delete(
        self, belief_id: UUID, principal: Principal, tombstone: BeliefRejection
    ) -> None:
        result = await self._session.execute(
            delete(MemoryRow).where(
                MemoryRow.id == belief_id,
                MemoryRow.tenant_id == principal.tenant_id,
                MemoryRow.principal_id == principal.principal_id,
                ~MemoryRow.erasure_pending,
            )
        )
        if not _rowcount(result):
            raise NotFoundError("memory not found")
        await self._erase_rejections(principal, [belief_id])
        await self._session.execute(
            pg_insert(MemoryRejectionRow).values(**_rejection_values(erased_rejection(tombstone)))
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
                        ~MemoryRow.erasure_pending,
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
                    "status": (
                        MemoryStatus.RETIRED
                        if row.lifecycle_policy_version == LIFECYCLE_POLICY_VERSION
                        else MemoryStatus.EXPIRED
                    ),
                    "valid_to": now,
                    "store_position": await self.next_position(),
                    "updated_at": now,
                }
            )
            await self.reinforce(value)
            expired.append(value)
        return expired


class PostgresTraceStore:
    def __init__(self, session: AsyncSession, people: PeopleStore | None = None) -> None:
        self._session = session
        self._people = people

    async def erase_people(
        self, principal: Principal, record_ids: Sequence[UUID], belief_ids: Sequence[UUID] = ()
    ) -> int:
        predicates = [
            RecallTraceRow.trace.contains({"people": [{field: value}]})
            for record_id in record_ids
            for field, value in (
                ("record_id", str(record_id)),
                ("person_ids", [str(record_id)]),
                ("source_ids", [str(record_id)]),
            )
        ]
        predicates.extend(
            RecallTraceRow.trace.contains({"returned": [str(belief_id)]})
            for belief_id in belief_ids
        )
        predicates.extend(
            RecallTraceRow.trace.contains({"query": {"people_scope": [str(record_id)]}})
            for record_id in record_ids
        )
        predicates.extend(
            RecallTraceRow.trace.contains({"query": {field: [str(belief_id)]}})
            for belief_id in belief_ids
            for field in ("include_ids", "expand_ids")
        )
        if not predicates:
            return 0
        result = await self._session.execute(
            delete(RecallTraceRow).where(
                RecallTraceRow.tenant_id == principal.tenant_id,
                RecallTraceRow.principal_id == principal.principal_id,
                or_(*predicates),
            )
        )
        return _rowcount(result)

    async def record(self, trace: RecallTrace) -> None:
        if (
            trace.run_id is not None
            and await lock_run_erasure(self._session, trace.run_id) is not None
        ):
            raise RunCancelledError("People erasure fenced recall trace writes")
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
            if row is None or row.erasure_pending or RecallTrace.model_validate(row.trace) != trace:
                raise ConflictError("trace id identifies different content")

    async def for_turn(self, turn_id: UUID) -> list[RecallTrace]:
        rows = list(
            (
                await self._session.scalars(
                    select(RecallTraceRow)
                    .where(RecallTraceRow.turn_id == turn_id, ~RecallTraceRow.erasure_pending)
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
                    ~RecallTraceRow.erasure_pending,
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
                    ~RecallTraceRow.erasure_pending,
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
        people: list[TracedPersonContext] = []
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
            if self._people is not None:
                owner = Principal(tenant_id=trace.tenant_id, principal_id=trace.principal_id)
                for person_item in trace.people:
                    live = await self._people.get(owner, person_item.record_id, ceiling=ceiling)
                    if live is not None and SENSITIVITY_ORDER[person_item.sensitivity] <= effective:
                        people.append(person_item)
        as_of = max(
            (trace.created_at for trace in traces),
            default=datetime.min.replace(tzinfo=UTC),
        )
        return RecallTraceView(
            turn_id=turn_id,
            moments=[trace.moment for trace in traces],
            beliefs=beliefs,
            people=people[:20],
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


def _knowledge_source_visible() -> ColumnElement[bool]:
    return (
        ~select(ArtifactRow.id)
        .join(RunRow, RunRow.id == ArtifactRow.run_id)
        .where(
            ArtifactRow.id == KnowledgeDocumentRow.source_artifact_id,
            ArtifactRow.origin != "upload",
            RunRow.people_erased_at.is_not(None),
        )
        .correlate(KnowledgeDocumentRow)
        .exists()
    )


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
        if (
            document.source_ref.origin != "upload"
            and document.source_ref.run_id is not None
            and await lock_run_erasure(self._session, document.source_ref.run_id) is not None
        ):
            raise RunCancelledError("People erasure fenced knowledge ingestion")
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
                    _knowledge_source_visible(),
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
                        _knowledge_source_visible(),
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
        row = await self._session.scalar(
            select(KnowledgeChunkRow)
            .join(
                KnowledgeDocumentRow,
                KnowledgeDocumentRow.row_id == KnowledgeChunkRow.document_row_id,
            )
            .where(KnowledgeChunkRow.chunk_id == chunk_id, _knowledge_source_visible())
        )
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
