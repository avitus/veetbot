"""Principal-explicit application service contracts shared by entry points."""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.browser import (
    BrowserActionKind,
    BrowserAuthenticationView,
    BrowserGrantView,
    BrowserProfileView,
)
from agent_core.domain.schedules import (
    ScheduleDefinition,
    ScheduleOccurrence,
    ScheduleRecord,
)
from agent_core.domain.views import (
    ApprovalFilters,
    ApprovalView,
    ArtifactContent,
    ArtifactView,
    CancelResult,
    ContentBlock,
    Page,
    RunView,
    SessionMessageView,
    SessionView,
    StreamFrame,
    SubmitResult,
)


class SessionService(Protocol):
    async def create(
        self, principal: Principal, agent_id: str, metadata: dict[str, object]
    ) -> SessionView: ...

    async def get(self, principal: Principal, session_id: UUID) -> SessionView: ...

    async def list(
        self, principal: Principal, limit: int, cursor: str | None
    ) -> Page[SessionView]: ...

    async def messages(
        self,
        principal: Principal,
        session_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> Page[SessionMessageView]: ...

    async def delete(self, principal: Principal, session_id: UUID) -> None: ...

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


class BrowserProfileService(Protocol):
    async def create(
        self,
        principal: Principal,
        allowed_origins: tuple[str, ...],
        idempotency_key: str | None = None,
    ) -> BrowserProfileView: ...

    async def get(self, principal: Principal, profile_id: UUID) -> BrowserProfileView: ...

    async def list(self, principal: Principal) -> list[BrowserProfileView]: ...

    async def revoke(self, principal: Principal, profile_id: UUID) -> BrowserProfileView: ...

    async def delete(self, principal: Principal, profile_id: UUID) -> None: ...

    async def begin_authentication(
        self,
        principal: Principal,
        profile_id: UUID,
        *,
        login_url: str,
        idempotency_key: str | None = None,
    ) -> BrowserAuthenticationView: ...

    async def list_authentications(
        self,
        principal: Principal,
        profile_id: UUID,
    ) -> builtins.list[BrowserAuthenticationView]: ...

    async def authentication_status(
        self,
        principal: Principal,
        authentication_id: UUID,
    ) -> BrowserAuthenticationView: ...

    async def cancel_authentication(
        self,
        principal: Principal,
        authentication_id: UUID,
    ) -> BrowserAuthenticationView: ...


class BrowserGrantService(Protocol):
    async def create(
        self,
        principal: Principal,
        *,
        profile_id: UUID,
        allowed_origins: tuple[str, ...],
        action_kinds: tuple[BrowserActionKind, ...],
        element_roles: tuple[str, ...],
        element_names: tuple[str, ...],
        purpose: str | None,
        starts_at: datetime,
        expires_at: datetime,
        idempotency_key: str | None = None,
    ) -> BrowserGrantView: ...

    async def get(self, principal: Principal, grant_id: UUID) -> BrowserGrantView: ...

    async def list(
        self,
        principal: Principal,
        *,
        profile_id: UUID | None = None,
    ) -> list[BrowserGrantView]: ...

    async def revoke(self, principal: Principal, grant_id: UUID) -> BrowserGrantView: ...

    async def delete(self, principal: Principal, grant_id: UUID) -> None: ...


class ScheduleService(Protocol):
    async def create(
        self,
        principal: Principal,
        definition: ScheduleDefinition,
        idempotency_key: str,
    ) -> ScheduleRecord: ...

    async def get(self, principal: Principal, schedule_id: UUID) -> ScheduleRecord: ...

    async def list(
        self, principal: Principal, limit: int, cursor: str | None
    ) -> Page[ScheduleRecord]: ...

    async def update(
        self,
        principal: Principal,
        schedule_id: UUID,
        expected_revision: int,
        definition: ScheduleDefinition,
    ) -> ScheduleRecord: ...

    async def pause(
        self, principal: Principal, schedule_id: UUID, expected_revision: int
    ) -> ScheduleRecord: ...

    async def resume(
        self, principal: Principal, schedule_id: UUID, expected_revision: int
    ) -> ScheduleRecord: ...

    async def cancel(
        self, principal: Principal, schedule_id: UUID, expected_revision: int
    ) -> ScheduleRecord: ...

    async def list_occurrences(
        self,
        principal: Principal,
        schedule_id: UUID,
        *,
        limit: int,
        cursor: str | None,
    ) -> Page[ScheduleOccurrence]: ...
