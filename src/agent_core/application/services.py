"""Principal-explicit application service contracts shared by entry points."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.views import (
    ApprovalFilters,
    ApprovalView,
    ArtifactContent,
    ArtifactView,
    CancelResult,
    ContentBlock,
    Page,
    RunView,
    SessionView,
    StreamFrame,
    SubmitResult,
)


class SessionService(Protocol):
    async def create(
        self, principal: Principal, agent_id: str, metadata: dict[str, object]
    ) -> SessionView: ...

    async def get(self, principal: Principal, session_id: UUID) -> SessionView: ...

    async def close(self, principal: Principal, session_id: UUID) -> SessionView: ...


class RunService(Protocol):
    async def submit(
        self,
        principal: Principal,
        session_id: UUID,
        content: list[ContentBlock],
        idempotency_key: str | None,
        trace_id: str | None,
    ) -> SubmitResult: ...

    async def get(self, principal: Principal, run_id: UUID) -> RunView: ...

    async def cancel(self, principal: Principal, run_id: UUID) -> CancelResult: ...

    async def deliver_input(
        self,
        principal: Principal,
        run_id: UUID,
        content: list[ContentBlock],
        question_id: UUID | None,
    ) -> SubmitResult: ...

    def stream(
        self,
        principal: Principal,
        run_id: UUID,
        after_sequence: int | None,
    ) -> AsyncIterator[StreamFrame]: ...


class ApprovalService(Protocol):
    async def list(
        self,
        principal: Principal,
        filters: ApprovalFilters,
        limit: int,
        cursor: str | None,
    ) -> Page[ApprovalView]: ...

    async def get(self, principal: Principal, approval_id: UUID) -> ApprovalView: ...

    async def resolve(
        self,
        principal: Principal,
        approval_id: UUID,
        decision: ApprovalResolutionType,
        reason: str | None,
    ) -> ApprovalView: ...


class ArtifactService(Protocol):
    async def get(self, principal: Principal, artifact_id: UUID) -> ArtifactView: ...

    async def open_content(self, principal: Principal, artifact_id: UUID) -> ArtifactContent: ...
