"""Durable principal-isolated email projections and concurrency boundary."""

from __future__ import annotations

import builtins
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.context import ContextPlan
from agent_core.domain.email import EmailDraft, EmailFeedback, EmailRecord, EmailTask, EmailThread
from agent_core.domain.email_semantics import EmailSemanticFact, EmailSemanticSource
from agent_core.domain.memory import MemoryRecord
from agent_core.domain.messages import ConversationItem
from agent_core.domain.persistence import WorkerLease
from agent_core.domain.runs import Run


class EmailStore(Protocol):
    """Records are private to one principal; missing/cross-owner reads return None.

    A principal lock serializes admission, budget reservation, source changes,
    draft edits and dispatch claims. SQL holds its lock to transaction completion;
    the in-memory implementation holds it to context exit. Callers perform no
    network calls while holding it. `put` requires the exact existing revision
    (zero means absent) and a record with revision expected+1 or raises ConflictError.
    Records returned to callers cannot mutate the durable store by aliasing.
    """

    def lock(self, principal: Principal) -> AbstractAsyncContextManager[None]: ...

    async def get(self, principal: Principal, kind: str, key: str) -> EmailRecord | None: ...

    async def list(
        self, principal: Principal, kind: str, *, after: str | None = None, limit: int = 1000
    ) -> list[EmailRecord]: ...

    async def list_tasks(
        self,
        principal: Principal,
        *,
        created_since: datetime | None = None,
        after: str | None = None,
        limit: int = 1000,
    ) -> builtins.list[EmailRecord]:
        """Unsettled tasks, plus tasks created since the optional accounting cutoff."""
        ...

    async def put(self, record: EmailRecord, *, expected_revision: int) -> EmailRecord: ...

    async def delete(
        self, principal: Principal, kind: str, key: str, *, expected_revision: int
    ) -> None: ...


class EmailRuntimeServices(Protocol):
    """Scoped projection and personalization boundary used by governed tasks."""

    account_servers: dict[str, dict[str, str]]

    async def get_task(self, principal: Principal, run_id: UUID) -> EmailTask | None: ...

    async def _thread(
        self, store: EmailStore, principal: Principal, thread_id: UUID
    ) -> EmailThread: ...

    async def _draft(
        self, store: EmailStore, principal: Principal, draft_id: UUID
    ) -> EmailDraft: ...

    async def _feedback(self, store: EmailStore, principal: Principal) -> list[EmailFeedback]: ...

    async def import_thread(
        self,
        principal: Principal,
        account_id: str,
        normalized: dict[str, Any],
        source_session_id: UUID,
        *,
        run: Run | None = None,
        lease: WorkerLease | None = None,
    ) -> EmailThread: ...

    async def save_assessment(
        self,
        principal: Principal,
        thread_id: UUID,
        source_revision: int,
        assessment: dict[str, Any],
        *,
        run: Run | None = None,
        lease: WorkerLease | None = None,
    ) -> EmailThread: ...

    async def save_generated_draft(
        self,
        principal: Principal,
        thread_id: UUID,
        source_revision: int,
        body: str,
        *,
        run_id: UUID,
        instruction: str | None = None,
        run: Run | None = None,
        lease: WorkerLease | None = None,
    ) -> EmailDraft: ...

    async def learning_context(
        self,
        principal: Principal,
        thread: EmailThread | None,
    ) -> dict[str, Any]: ...


class EmailSemanticPort(Protocol):
    """Attributed source registration and separately evaluated fact formation."""

    @property
    def enabled(self) -> bool: ...

    async def register_source(
        self,
        source: EmailSemanticSource,
        *,
        run: Run | None = None,
        lease: WorkerLease | None = None,
    ) -> None: ...

    async def form(
        self,
        source: EmailSemanticSource,
        facts: Sequence[EmailSemanticFact],
        *,
        scope: str = "general",
        run_id: UUID | None = None,
        run: Run | None = None,
        lease: WorkerLease | None = None,
    ) -> list[MemoryRecord]: ...


class EmailContextRenderer(Protocol):
    """Canonical trusted prefix and escaped untrusted evidence, composed externally."""

    def __call__(
        self,
        agent: AgentSpec,
        context: ContextPlan,
        instruction: str,
        data: dict[str, Any],
    ) -> tuple[list[ConversationItem], list[ConversationItem]]: ...
