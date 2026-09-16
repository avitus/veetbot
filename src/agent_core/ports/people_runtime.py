"""Composition seams for People formation, recall, and import orchestration."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.email import EmailRecord
from agent_core.domain.email_semantics import EmailSemanticSource
from agent_core.domain.events import EventEnvelope
from agent_core.domain.memory import (
    ConsolidationResult,
    MemoryRecord,
    RecallQuery,
    RecallResult,
    Sensitivity,
    TracedPersonContext,
)
from agent_core.domain.people import PeopleImportJob
from agent_core.domain.people_imports import PeopleImportScope
from agent_core.domain.persistence import WorkerLease
from agent_core.domain.runs import Run
from agent_core.ports.determinism import Clock
from agent_core.ports.email import EmailSemanticPort
from agent_core.ports.memory import MemoryRetriever
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory


class PeopleOwnerCorrections(Protocol):
    async def correct_from_owner(
        self,
        belief_id: UUID,
        *,
        session_id: UUID,
        source_event_id: int,
        operation: str,
        statement: str | None,
        expected_position: int,
        existing_uow: RepositoryUnitOfWork,
        effective_at: datetime | None = None,
    ) -> MemoryRecord | None: ...


class PeopleRecall(MemoryRetriever, Protocol):
    def current_time(self) -> datetime: ...

    async def recall(
        self,
        query: RecallQuery,
        *,
        session_id: UUID,
        run_id: UUID | None = None,
        turn_id: UUID | None = None,
        moment: str = "in_turn",
        surface_id: str = "private",
        measure_rendered_tokens: Callable[[str], int] | None = None,
        people_items: list[TracedPersonContext] | None = None,
        existing_uow: RepositoryUnitOfWork | None = None,
    ) -> RecallResult: ...


class PeopleImportFormation(Protocol):
    async def run(
        self,
        *,
        trigger: str,
        scope: str,
        session_id: UUID | None,
        since_watermark: int | None = None,
        source_window: tuple[EventEnvelope, ...] | None = None,
    ) -> ConsolidationResult: ...


class PeopleImportControl(Protocol):
    factory: UnitOfWorkFactory
    clock: Clock
    capture_available: bool
    email_capture_available: bool
    account_servers: dict[str, dict[str, str]]
    dispatch: Callable[[UUID], Awaitable[None]] | None
    implementation_identity: Callable[[], str]
    source_text: Callable[[EventEnvelope, Principal], str | None]

    async def _get(
        self,
        uow: RepositoryUnitOfWork,
        owner: Principal,
        key: UUID,
        ceiling: Sensitivity,
    ) -> PeopleImportJob: ...

    async def validate_sources(
        self,
        uow: RepositoryUnitOfWork,
        owner: Principal,
        scope: PeopleImportScope,
        ceiling: Sensitivity,
    ) -> None: ...

    async def enqueue(
        self,
        uow: RepositoryUnitOfWork,
        owner: Principal,
        job: PeopleImportJob,
    ) -> PeopleImportJob: ...


class PeopleEmailImportSemantics(EmailSemanticPort, Protocol):
    async def retain_source(
        self, source: EmailSemanticSource, *, run: Run, lease: WorkerLease | None
    ) -> None: ...

    async def import_passage(
        self,
        record: EmailRecord,
        *,
        after_offset: int | None,
    ) -> EmailSemanticSource | None: ...
