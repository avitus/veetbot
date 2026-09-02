"""Long-term memory formation, retrieval, and trace ports."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.context import WorkingState
from agent_core.domain.events import EventEnvelope
from agent_core.domain.memory import (
    BeliefRejection,
    BeliefType,
    ConsolidationResult,
    ConsolidationRun,
    EpisodeQuery,
    IntegratedEpisode,
    MemoryAuthority,
    MemoryBrowseQuery,
    MemoryCandidate,
    MemoryCorrection,
    MemoryEdit,
    MemoryExtractionResult,
    MemoryRecord,
    Polarity,
    RecalledBelief,
    RecallQuery,
    RecallResult,
    RecallTrace,
    RecallTraceView,
)
from agent_core.domain.runs import Run


class MemoryStore(Protocol):
    async def next_position(self) -> int: ...

    async def head_position(self, principal: Principal) -> int: ...

    async def get(self, belief_id: UUID, principal: Principal) -> MemoryRecord: ...

    async def query(self, query: RecallQuery) -> list[MemoryRecord]: ...

    async def related(
        self,
        tenant_id: str,
        principal_id: str,
        subject: str,
        belief_type: BeliefType,
    ) -> list[MemoryRecord]: ...

    async def upsert_belief(self, belief: MemoryRecord) -> MemoryRecord: ...

    async def reinforce(self, belief: MemoryRecord) -> MemoryRecord: ...

    async def supersede(
        self, current: MemoryRecord, replacement: MemoryRecord
    ) -> tuple[MemoryRecord, MemoryRecord]: ...

    async def list_memories(
        self,
        principal: Principal,
        *,
        include_inactive: bool = False,
        session_id: UUID | None = None,
        limit: int = 200,
    ) -> list[MemoryRecord]: ...

    async def browse(self, query: MemoryBrowseQuery) -> list[MemoryRecord]:
        """Return up to ``query.limit + 1`` records, newest store position first.

        The extra row is the has-more probe used by the paging service; an
        adapter that returns exactly ``query.limit`` rows would truncate the
        walk by suppressing a non-final page's next cursor.
        """
        ...

    async def list_idle(
        self,
        principal: Principal,
        *,
        evidence_before: datetime,
        decay_confidence_ceiling: float | None = None,
        limit: int,
    ) -> list[MemoryRecord]: ...

    async def edit(
        self, belief_id: UUID, principal: Principal, edit: MemoryEdit, edited: MemoryRecord
    ) -> MemoryRecord: ...

    async def delete(
        self, belief_id: UUID, principal: Principal, tombstone: BeliefRejection
    ) -> None: ...

    async def reject(self, rejection: BeliefRejection, updated: MemoryRecord) -> MemoryRecord: ...

    async def outstanding_rejections(
        self, tenant_id: str, principal_id: str
    ) -> list[BeliefRejection]: ...

    async def record_consolidation(self, run: ConsolidationRun) -> ConsolidationRun: ...

    async def list_consolidations(
        self,
        principal: Principal,
        *,
        session_id: UUID | None = None,
        limit: int = 100,
    ) -> list[ConsolidationRun]: ...

    async def consolidation_watermark(self, session_id: UUID, principal: Principal) -> int: ...

    async def set_consolidation_watermark(
        self, session_id: UUID, principal: Principal, sequence: int
    ) -> None: ...

    async def expire(self, principal: Principal) -> list[MemoryRecord]: ...


class IntegratedEpisodeStore(Protocol):
    async def put(self, episode: IntegratedEpisode) -> IntegratedEpisode: ...

    async def get(self, episode_id: UUID, principal: Principal) -> IntegratedEpisode: ...

    async def for_session(
        self,
        session_id: UUID,
        principal: Principal,
        *,
        limit: int = 100,
    ) -> list[IntegratedEpisode]: ...

    async def delete_for_session(self, session_id: UUID, principal: Principal) -> int: ...

    async def delete_for_principal(self, principal: Principal) -> int: ...


class MemoryConsolidator(Protocol):
    async def run(
        self,
        *,
        trigger: str,
        scope: str,
        session_id: UUID | None,
        since_watermark: int | None = None,
    ) -> ConsolidationResult: ...


class MemoryCandidateExtractor(Protocol):
    name: str

    async def extract(
        self,
        events: list[EventEnvelope],
        *,
        principal: Principal,
        scope: str,
    ) -> list[MemoryCandidate] | MemoryExtractionResult: ...


class Salience(Protocol):
    def eligible(self, statement: str, *, explicit: bool) -> bool: ...


class ConflictResolver(Protocol):
    def relationship(
        self,
        existing: MemoryRecord,
        statement: str,
        source_event_ids: list[int],
        *,
        authority: MemoryAuthority = MemoryAuthority.USER,
        polarity: Polarity = Polarity.ASSERT,
        session_id: UUID | None = None,
        at: datetime | None = None,
    ) -> str: ...


class MemoryRetriever(Protocol):
    async def recall(
        self,
        query: RecallQuery,
        *,
        session_id: UUID,
        run_id: UUID | None = None,
        turn_id: UUID | None = None,
        moment: str = "in_turn",
        surface_id: str = "private",
    ) -> RecallResult: ...

    async def corrections(
        self,
        *,
        snapshot_id: UUID,
        watermark: int,
        as_of: datetime | None = None,
    ) -> list[MemoryCorrection]: ...


class QueryFormer(Protocol):
    def form(
        self,
        run: Run,
        working_state: WorkingState,
        message: str | None,
        *,
        current_scope: str | None = None,
    ) -> list[RecallQuery]: ...


class Ranker(Protocol):
    def rank(
        self, candidates: list[RecalledBelief], query: RecallQuery
    ) -> list[RecalledBelief]: ...


class EpisodeSearch(Protocol):
    async def search(self, query: EpisodeQuery) -> list[EventEnvelope]: ...


class TraceStore(Protocol):
    async def record(self, trace: RecallTrace) -> None: ...

    async def for_turn(self, turn_id: UUID) -> list[RecallTrace]: ...

    async def get(self, trace_id: UUID, principal: Principal) -> RecallTrace: ...

    async def mark_cited(
        self, trace_id: UUID, principal: Principal, cited: Sequence[UUID]
    ) -> RecallTrace: ...

    async def user_view(
        self, turn_id: UUID, viewing_surface_id: str, viewing_ceiling: str
    ) -> RecallTraceView: ...

    async def expire_operator_fields(self, now: datetime, limit: int) -> int: ...

    async def mark_document_deleted(self, tenant_id: str, document_id: UUID) -> None: ...
