"""Durable principal-isolated email projections and concurrency boundary."""

from __future__ import annotations

import builtins
from collections.abc import Iterable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.context import ContextPlan
from agent_core.domain.correspondence import CorrespondenceSummaryWork, EmailCorrespondenceSummary
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

    async def list_thread_summaries(
        self, principal: Principal, *, after: str | None = None, limit: int = 1000
    ) -> builtins.list[EmailRecord]:
        """Key-ordered `thread` records whose payloads omit `messages`.

        Inbox listing never needs message content, so an adapter may avoid
        reading or decoding it.
        """
        ...

    async def list_semantic_window(
        self,
        principal: Principal,
        *,
        account_ids: Sequence[str],
        since: datetime,
        until: datetime,
        after: tuple[datetime, str] | None = None,
        limit: int = 100,
    ) -> builtins.list[EmailRecord]: ...

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

    async def belief_messages(
        self, principal: Principal, belief_ids: Sequence[UUID]
    ) -> builtins.list[tuple[str, str, str]]:
        """The (account, thread, message) of each retained source that formed these beliefs."""
        ...

    async def fence_people_erasure(
        self, principal: Principal, belief_ids: Sequence[UUID], erased_at: datetime
    ) -> int:
        """Fence linked generated copies and invalidate stale projection revisions."""
        ...

    async def purge_people_erasure(self, principal: Principal) -> bool:
        """Purge at most 256 fenced payloads; return whether more remain."""
        ...


class EmailSubscriptionRuntime(Protocol):
    """Census and consent boundary used by the refresh and subscription tasks.

    Every method is a no-op or a refusal when unsubscribe assistance is disabled.
    Writes made under a worker lease are fenced by it.
    """

    async def observe(
        self,
        principal: Principal,
        account_id: str,
        summaries: Iterable[dict[str, Any]],
        *,
        run: Run | None = None,
        lease: WorkerLease | None = None,
    ) -> int: ...

    async def sweep(
        self,
        principal: Principal,
        account_id: str,
        *,
        run: Run | None = None,
        lease: WorkerLease | None = None,
    ) -> None: ...

    async def unverified(self, principal: Principal, account_id: str) -> list[tuple[str, str]]: ...

    async def apply_verification(
        self,
        principal: Principal,
        subscription_id: str,
        block: dict[str, Any],
        *,
        run: Run | None = None,
        lease: WorkerLease | None = None,
    ) -> None: ...

    async def validate(
        self, principal: Principal, run: Run, lease: WorkerLease | None
    ) -> EmailTask: ...

    async def approve(
        self, principal: Principal, run: Run, lease: WorkerLease | None, approval_id: UUID
    ) -> None: ...

    async def settle(
        self,
        principal: Principal,
        run: Run,
        lease: WorkerLease | None,
        subscription_id: str,
        *,
        action: Literal["unsubscribe", "report_spam", "not_spam"],
        status: Literal["completed", "failed", "uncertain"],
        code: str,
        thread_ids: list[str] | None = None,
    ) -> None: ...

    async def finish(self, principal: Principal, run: Run, lease: WorkerLease | None) -> None: ...


class EmailRuntimeServices(Protocol):
    """Scoped projection and personalization boundary used by governed tasks."""

    account_servers: dict[str, dict[str, str]]

    @property
    def subscriptions(self) -> EmailSubscriptionRuntime: ...

    async def get_task(self, principal: Principal, run_id: UUID) -> EmailTask | None: ...

    async def validate_archive(
        self,
        principal: Principal,
        run: Run,
        lease: WorkerLease | None,
    ) -> EmailTask: ...

    async def approve_archive(
        self,
        principal: Principal,
        run: Run,
        lease: WorkerLease | None,
        approval_id: UUID,
    ) -> None: ...

    async def finish_archive(
        self,
        principal: Principal,
        run: Run,
        lease: WorkerLease | None,
        *,
        status: Literal["completed", "failed", "uncertain"],
    ) -> None: ...

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

    @property
    def people_enabled(self) -> bool: ...

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

    async def next_correspondence_summaries(
        self, account_ids: Sequence[str], *, limit: int
    ) -> list[CorrespondenceSummaryWork]:
        """Observed exchanges awaiting a short summary, newest first (ADR-0126)."""
        ...

    async def record_correspondence_summary(
        self,
        work: CorrespondenceSummaryWork,
        result: EmailCorrespondenceSummary | None,
        *,
        model: str,
        run: Run | None = None,
        lease: WorkerLease | None = None,
    ) -> str:
        """Store a valid summary or count an invalid one; returns the resulting state."""
        ...


class EmailContextRenderer(Protocol):
    """Canonical trusted prefix and escaped untrusted evidence, composed externally."""

    def __call__(
        self,
        agent: AgentSpec,
        context: ContextPlan,
        instruction: str,
        data: dict[str, Any],
    ) -> tuple[list[ConversationItem], list[ConversationItem]]: ...
