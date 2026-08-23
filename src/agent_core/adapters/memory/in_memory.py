"""Deterministic in-memory belief, trace, and knowledge stores."""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.knowledge import (
    DocumentAuthority,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngestPrepared,
    KnowledgeQuery,
    RetrievedPassage,
)
from agent_core.domain.memory import (
    SENSITIVITY_ORDER,
    BeliefRejection,
    BeliefType,
    ConsolidationRun,
    MemoryEdit,
    MemoryRecord,
    MemoryStatus,
    RecallQuery,
    RecallTrace,
    RecallTraceView,
    Sensitivity,
    TracedBelief,
    TracedPassage,
    lexical_query_terms,
    lexical_term_lexemes,
    lexical_text_matches,
)
from agent_core.domain.trajectory import ArtifactRef
from agent_core.ports.determinism import Clock

_LIVE_MEMORY = frozenset({MemoryStatus.ACTIVE, MemoryStatus.PROVISIONAL})


class InMemoryMemoryStore:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._records: dict[UUID, MemoryRecord] = {}
        self._rejections: dict[UUID, BeliefRejection] = {}
        self._consolidations: dict[UUID, ConsolidationRun] = {}
        self._watermarks: dict[tuple[str, str, UUID], int] = {}
        self._position = 0
        self._lock = asyncio.Lock()

    async def next_position(self) -> int:
        async with self._lock:
            self._position += 1
            return self._position

    async def head_position(self, principal: Principal) -> int:
        async with self._lock:
            return max(
                (
                    record.store_position
                    for record in self._records.values()
                    if record.tenant_id == principal.tenant_id
                    and record.principal_id == principal.principal_id
                ),
                default=0,
            )

    async def get(self, belief_id: UUID, principal: Principal) -> MemoryRecord:
        async with self._lock:
            record = self._records.get(belief_id)
            if record is None or (
                record.tenant_id != principal.tenant_id
                or record.principal_id != principal.principal_id
            ):
                raise NotFoundError("memory not found")
            return record.model_copy(deep=True)

    async def query(self, query: RecallQuery) -> list[MemoryRecord]:
        as_of = query.as_of or self._clock.now()
        term_lexemes = lexical_term_lexemes(lexical_query_terms(query.text))
        subjects = {subject.casefold() for subject in query.subjects}
        async with self._lock:
            result = []
            for record in self._records.values():
                if record.tenant_id != query.tenant_id or record.principal_id != query.principal_id:
                    continue
                if record.store_position <= query.min_store_position:
                    continue
                if (
                    not query.include_superseded
                    and query.as_of is None
                    and record.status not in _LIVE_MEMORY
                ):
                    continue
                if record.valid_from > as_of or (
                    record.valid_to is not None and record.valid_to <= as_of
                ):
                    continue
                if record.expires_at is not None and record.expires_at <= as_of:
                    continue
                if (
                    SENSITIVITY_ORDER[record.sensitivity]
                    > SENSITIVITY_ORDER[query.sensitivity_ceiling]
                ):
                    continue
                if query.belief_types and record.belief_type not in query.belief_types:
                    continue
                if (
                    record.portability.value == "local"
                    and record.scope != query.current_scope
                    and record.subject.casefold() not in subjects
                ):
                    continue
                if term_lexemes and record.subject.casefold() not in subjects:
                    text = f"{record.subject} {record.statement}"
                    if not lexical_text_matches(term_lexemes, text):
                        continue
                result.append(record.model_copy(deep=True))
            result.sort(key=lambda record: (-record.store_position, str(record.id)))
            return result[: max(query.max_items * 8, 64)]

    async def related(
        self,
        tenant_id: str,
        principal_id: str,
        subject: str,
        belief_type: BeliefType,
    ) -> list[MemoryRecord]:
        async with self._lock:
            return [
                record.model_copy(deep=True)
                for record in self._records.values()
                if record.tenant_id == tenant_id
                and record.principal_id == principal_id
                and record.subject.casefold() == subject.casefold()
                and record.belief_type is belief_type
                and record.status in _LIVE_MEMORY
            ]

    async def upsert_belief(self, belief: MemoryRecord) -> MemoryRecord:
        async with self._lock:
            existing = self._records.get(belief.id)
            if existing is not None and existing != belief:
                raise ConflictError("memory id identifies different content")
            self._records[belief.id] = belief.model_copy(deep=True)
            self._position = max(self._position, belief.store_position)
            return belief.model_copy(deep=True)

    async def reinforce(self, belief: MemoryRecord) -> MemoryRecord:
        async with self._lock:
            if belief.id not in self._records:
                raise NotFoundError("memory not found")
            self._records[belief.id] = belief.model_copy(deep=True)
            self._position = max(self._position, belief.store_position)
            return belief.model_copy(deep=True)

    async def supersede(
        self, current: MemoryRecord, replacement: MemoryRecord
    ) -> tuple[MemoryRecord, MemoryRecord]:
        async with self._lock:
            existing = self._records.get(current.id)
            if existing is None:
                raise NotFoundError("memory not found")
            if existing.status not in _LIVE_MEMORY:
                raise ConflictError("memory was already inactive")
            self._records[current.id] = current.model_copy(deep=True)
            self._records[replacement.id] = replacement.model_copy(deep=True)
            self._position = max(self._position, current.store_position, replacement.store_position)
            return current.model_copy(deep=True), replacement.model_copy(deep=True)

    async def list_memories(
        self,
        principal: Principal,
        *,
        include_inactive: bool = False,
        session_id: UUID | None = None,
        limit: int = 200,
    ) -> list[MemoryRecord]:
        async with self._lock:
            records = [
                record
                for record in self._records.values()
                if record.tenant_id == principal.tenant_id
                and record.principal_id == principal.principal_id
                and (include_inactive or record.status in _LIVE_MEMORY)
                and (session_id is None or record.source_session_id == session_id)
            ]
            records.sort(key=lambda item: (-item.store_position, str(item.id)))
            return [item.model_copy(deep=True) for item in records[:limit]]

    async def list_idle(
        self,
        principal: Principal,
        *,
        reinforced_before: datetime,
        limit: int,
    ) -> list[MemoryRecord]:
        async with self._lock:
            records = [
                record
                for record in self._records.values()
                if record.tenant_id == principal.tenant_id
                and record.principal_id == principal.principal_id
                and record.status in _LIVE_MEMORY
                and record.last_reinforced_at <= reinforced_before
            ]
            records.sort(key=lambda item: (item.last_reinforced_at, str(item.id)))
            return [item.model_copy(deep=True) for item in records[:limit]]

    async def edit(
        self, belief_id: UUID, principal: Principal, edit: MemoryEdit, edited: MemoryRecord
    ) -> MemoryRecord:
        del edit
        async with self._lock:
            current = self._records.get(belief_id)
            if current is None or (
                current.tenant_id != principal.tenant_id
                or current.principal_id != principal.principal_id
            ):
                raise NotFoundError("memory not found")
            self._records[belief_id] = edited.model_copy(deep=True)
            self._position = max(self._position, edited.store_position)
            return edited.model_copy(deep=True)

    async def delete(
        self, belief_id: UUID, principal: Principal, tombstone: BeliefRejection
    ) -> None:
        async with self._lock:
            current = self._records.get(belief_id)
            if current is None or (
                current.tenant_id != principal.tenant_id
                or current.principal_id != principal.principal_id
            ):
                raise NotFoundError("memory not found")
            del self._records[belief_id]
            self._rejections[tombstone.id] = tombstone.model_copy(deep=True)

    async def reject(self, rejection: BeliefRejection, updated: MemoryRecord) -> MemoryRecord:
        async with self._lock:
            if updated.id not in self._records:
                raise NotFoundError("memory not found")
            self._rejections[rejection.id] = rejection.model_copy(deep=True)
            self._records[updated.id] = updated.model_copy(deep=True)
            self._position = max(self._position, updated.store_position)
            return updated.model_copy(deep=True)

    async def outstanding_rejections(
        self, tenant_id: str, principal_id: str
    ) -> list[BeliefRejection]:
        async with self._lock:
            return [
                rejection.model_copy(deep=True)
                for rejection in self._rejections.values()
                if rejection.tenant_id == tenant_id and rejection.principal_id == principal_id
            ]

    async def record_consolidation(self, run: ConsolidationRun) -> ConsolidationRun:
        async with self._lock:
            self._consolidations[run.id] = run.model_copy(deep=True)
            return run.model_copy(deep=True)

    async def list_consolidations(
        self,
        principal: Principal,
        *,
        session_id: UUID | None = None,
        limit: int = 100,
    ) -> list[ConsolidationRun]:
        async with self._lock:
            runs = [
                run
                for run in self._consolidations.values()
                if run.tenant_id == principal.tenant_id
                and run.principal_id == principal.principal_id
                and (session_id is None or run.session_id == session_id)
            ]
            runs.sort(key=lambda item: (item.started_at, str(item.id)), reverse=True)
            return [item.model_copy(deep=True) for item in runs[:limit]]

    async def consolidation_watermark(self, session_id: UUID, principal: Principal) -> int:
        async with self._lock:
            return self._watermarks.get(
                (principal.tenant_id, principal.principal_id, session_id), 0
            )

    async def set_consolidation_watermark(
        self, session_id: UUID, principal: Principal, sequence: int
    ) -> None:
        async with self._lock:
            key = (principal.tenant_id, principal.principal_id, session_id)
            self._watermarks[key] = max(self._watermarks.get(key, 0), sequence)

    async def expire(self, principal: Principal) -> list[MemoryRecord]:
        now = self._clock.now()
        expired: list[MemoryRecord] = []
        async with self._lock:
            for belief_id, record in list(self._records.items()):
                if (
                    record.tenant_id != principal.tenant_id
                    or record.principal_id != principal.principal_id
                    or record.status not in _LIVE_MEMORY
                    or record.expires_at is None
                    or record.expires_at > now
                ):
                    continue
                self._position += 1
                updated = record.model_copy(
                    update={
                        "status": MemoryStatus.EXPIRED,
                        "valid_to": now,
                        "store_position": self._position,
                        "updated_at": now,
                    }
                )
                self._records[belief_id] = updated
                expired.append(updated.model_copy(deep=True))
        return expired


class InMemoryTraceStore:
    def __init__(self) -> None:
        self._traces: dict[UUID, RecallTrace] = {}
        self._lock = asyncio.Lock()

    async def record(self, trace: RecallTrace) -> None:
        async with self._lock:
            existing = self._traces.get(trace.id)
            if existing is not None and existing != trace:
                raise ConflictError("trace id identifies different content")
            self._traces[trace.id] = trace.model_copy(deep=True)

    async def for_turn(self, turn_id: UUID) -> list[RecallTrace]:
        async with self._lock:
            return [
                trace.model_copy(deep=True)
                for trace in self._traces.values()
                if trace.turn_id == turn_id
            ]

    async def get(self, trace_id: UUID, principal: Principal) -> RecallTrace:
        async with self._lock:
            trace = self._traces.get(trace_id)
            if trace is None or (
                trace.tenant_id != principal.tenant_id
                or trace.principal_id != principal.principal_id
            ):
                raise NotFoundError("recall trace not found")
            return trace.model_copy(deep=True)

    async def mark_cited(
        self, trace_id: UUID, principal: Principal, cited: Sequence[UUID]
    ) -> RecallTrace:
        async with self._lock:
            trace = self._traces.get(trace_id)
            if trace is None or (
                trace.tenant_id != principal.tenant_id
                or trace.principal_id != principal.principal_id
            ):
                raise NotFoundError("recall trace not found")
            marked = list(trace.cited)
            known = set(marked)
            for belief_id in cited:
                if belief_id not in known:
                    known.add(belief_id)
                    marked.append(belief_id)
            if marked != trace.cited:
                trace = trace.model_copy(update={"cited": marked})
                self._traces[trace_id] = trace
            return trace.model_copy(deep=True)

    async def expire_operator_fields(self, now: datetime, limit: int) -> int:
        if limit < 0:
            raise ValueError("recall-trace expiry limit must be nonnegative")
        async with self._lock:
            expired = sorted(
                (
                    trace
                    for trace in self._traces.values()
                    if trace.operator_fields_expire_at <= now and trace.has_operator_fields
                ),
                key=lambda trace: (trace.operator_fields_expire_at, str(trace.id)),
            )[:limit]
            for trace in expired:
                self._traces[trace.id] = trace.model_copy(
                    update={
                        "arm_latencies_ms": {},
                        "candidates": 0,
                        "dropped_for_budget": [],
                        "dropped_for_budget_count": (
                            trace.dropped_for_budget_count + len(trace.dropped_for_budget)
                        ),
                    }
                )
            return len(expired)

    async def user_view(
        self, turn_id: UUID, viewing_surface_id: str, viewing_ceiling: str
    ) -> RecallTraceView:
        del viewing_surface_id
        ceiling = Sensitivity(viewing_ceiling)
        traces = await self.for_turn(turn_id)
        beliefs: list[TracedBelief] = []
        passages: list[TracedPassage] = []
        considered = 0
        withheld = 0
        as_of = max(
            (trace.created_at for trace in traces),
            default=datetime.min.replace(tzinfo=UTC),
        )
        for trace in traces:
            effective = min(
                SENSITIVITY_ORDER[trace.sensitivity_ceiling], SENSITIVITY_ORDER[ceiling]
            )
            considered += trace.considered_not_shown
            withheld += len(trace.blocked)
            for item in trace.beliefs:
                if SENSITIVITY_ORDER[item.sensitivity] > effective or item.blocked:
                    continue
                beliefs.append(
                    TracedBelief(
                        belief_id=item.belief_id,
                        subject=item.subject,
                        statement=item.statement,
                        learned_at=item.valid_from,
                        origin_scope=item.origin_scope,
                        carried=item.carried,
                        authority=item.authority,
                        source_event_id=(
                            item.source_event_ids[0] if item.source_event_ids else None
                        ),
                        confidence_band=item.confidence_band,
                        used=item.belief_id in trace.cited,
                    )
                )
            passages.extend(
                passage.model_copy(deep=True)
                for passage in trace.passages
                if SENSITIVITY_ORDER[passage.sensitivity] <= effective
            )
        return RecallTraceView(
            turn_id=turn_id,
            moments=[trace.moment for trace in traces],
            beliefs=beliefs,
            passages=passages,
            considered_not_shown=considered,
            withheld_by_safety=withheld,
            as_of=as_of,
        )

    async def mark_document_deleted(self, tenant_id: str, document_id: UUID) -> None:
        async with self._lock:
            for trace_id, trace in list(self._traces.items()):
                if trace.tenant_id != tenant_id:
                    continue
                passages = [
                    passage.model_copy(update={"text": None, "deleted": True})
                    if passage.document_id == document_id
                    else passage
                    for passage in trace.passages
                ]
                if passages != trace.passages:
                    self._traces[trace_id] = trace.model_copy(update={"passages": passages})


class InMemoryKnowledgeStore:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._documents: dict[UUID, KnowledgeDocument] = {}
        self._chunks: dict[str, KnowledgeChunk] = {}
        self._lock = asyncio.Lock()

    async def ingest(self, prepared: KnowledgeIngestPrepared) -> None:
        async with self._lock:
            document = prepared.document
            if document.row_id in self._documents:
                raise ConflictError("knowledge document row already exists")
            if any(
                row.tenant_id == document.tenant_id
                and row.document_id == document.document_id
                and row.version == document.version
                for row in self._documents.values()
            ):
                raise ConflictError("knowledge document version already exists")
            for chunk in prepared.chunks:
                prior = self._chunks.get(chunk.chunk_id)
                if prior is not None and prior != chunk:
                    raise ConflictError("knowledge chunk id identifies different text")
            existing = [
                row
                for row in self._documents.values()
                if row.tenant_id == document.tenant_id
                and row.document_id == document.document_id
                and row.valid_to is None
            ]
            for row in existing:
                self._documents[row.row_id] = row.model_copy(
                    update={"superseded_by": document.row_id, "valid_to": document.valid_from}
                )
            self._documents[document.row_id] = document.model_copy(deep=True)
            for chunk in prepared.chunks:
                self._chunks[chunk.chunk_id] = chunk.model_copy(deep=True)

    async def latest(self, tenant_id: str, document_id: UUID) -> KnowledgeDocument | None:
        async with self._lock:
            rows = [
                row
                for row in self._documents.values()
                if row.tenant_id == tenant_id and row.document_id == document_id
            ]
            if not rows:
                return None
            return max(rows, key=lambda item: item.version).model_copy(deep=True)

    async def search(self, query: KnowledgeQuery) -> list[RetrievedPassage]:
        as_of = query.as_of or self._clock.now()
        terms = _terms(query.text)
        per_document: defaultdict[UUID, int] = defaultdict(int)
        scored: list[RetrievedPassage] = []
        async with self._lock:
            for chunk in self._chunks.values():
                document = self._documents.get(chunk.document_row_id)
                if document is None or document.tenant_id != query.tenant_id:
                    continue
                if document.valid_from > as_of or (
                    document.valid_to is not None and document.valid_to <= as_of
                ):
                    continue
                if not _visible(document, query):
                    continue
                if (
                    SENSITIVITY_ORDER[document.sensitivity]
                    > SENSITIVITY_ORDER[query.sensitivity_ceiling]
                ):
                    continue
                match = _lexical_score(terms, f"{' '.join(chunk.heading_path)} {chunk.text}")
                if match <= 0:
                    continue
                scope = 1.0 if document.project_scope == query.current_scope else 0.5
                authority = {
                    DocumentAuthority.PRINCIPAL_AUTHORED: 1.0,
                    DocumentAuthority.PRINCIPAL_SUPPLIED: 0.7,
                    DocumentAuthority.FETCHED: 0.4,
                }[document.authority]
                score = min(1.0, 0.75 * match + 0.15 * authority + 0.1 * scope)
                if score < query.min_score:
                    continue
                scored.append(
                    RetrievedPassage(
                        chunk_id=chunk.chunk_id,
                        document_id=document.document_id,
                        title=document.title,
                        heading_path=list(chunk.heading_path),
                        text=chunk.text,
                        doc_date=document.doc_date,
                        authority=document.authority,
                        sensitivity=document.sensitivity,
                        score=score,
                        arms=["lexical"],
                        instruction_like=chunk.contains_instruction_like_text,
                    )
                )
        result: list[RetrievedPassage] = []
        for passage in sorted(scored, key=lambda item: (-item.score, item.chunk_id)):
            if per_document[passage.document_id] >= query.max_per_document:
                continue
            per_document[passage.document_id] += 1
            result.append(passage)
            if len(result) >= query.max_passages:
                break
        return result

    async def get_chunk(self, chunk_id: str) -> KnowledgeChunk | None:
        async with self._lock:
            chunk = self._chunks.get(chunk_id)
            return None if chunk is None else chunk.model_copy(deep=True)

    async def delete(self, document_id: UUID, principal: Principal) -> list[ArtifactRef]:
        async with self._lock:
            rows = [
                row
                for row in self._documents.values()
                if row.document_id == document_id
                and row.tenant_id == principal.tenant_id
                and row.ingested_by_principal_id == principal.principal_id
            ]
            if not rows:
                raise NotFoundError("knowledge document not found")
            row_ids = {row.row_id for row in rows}
            refs = [row.source_ref.model_copy(deep=True) for row in rows]
            self._documents = {
                key: value for key, value in self._documents.items() if key not in row_ids
            }
            self._chunks = {
                key: value
                for key, value in self._chunks.items()
                if value.document_row_id not in row_ids
            }
            return refs


_PUNCTUATION = ".,:;!?()[]{}\"'"


def _tokens(text: str) -> list[str]:
    return "".join(
        " " if character in _PUNCTUATION else character for character in text.casefold()
    ).split()


def _terms(text: str) -> Counter[str]:
    return Counter(_tokens(text))


def _lexical_score(terms: Counter[str], text: str) -> float:
    if not terms:
        return 0
    words = Counter(_tokens(text))
    hits = sum(min(count, words[term]) for term, count in terms.items())
    return hits / sum(terms.values())


def _visible(document: KnowledgeDocument, query: KnowledgeQuery) -> bool:
    if document.visibility.value == "tenant":
        return True
    if document.visibility.value == "principal":
        return document.ingested_by_principal_id == query.principal_id
    return document.project_scope == query.current_scope
