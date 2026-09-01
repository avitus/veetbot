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
from agent_core.domain.devices import DeviceRegistration
from agent_core.domain.memory import BeliefType, MemoryStatus, Sensitivity
from agent_core.domain.persona import PersonaEntryDraft, PersonaNominationState
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
    DeviceRegistrationResult,
    DeviceView,
    MemoryView,
    NotificationInboxItem,
    Page,
    PersonaNominationView,
    PersonaView,
    RunView,
    SessionMessageView,
    SessionView,
    StreamFrame,
    SubmitResult,
    TestNotificationResult,
)


class SessionService(Protocol):
    async def create(
        self,
        principal: Principal,
        agent_id: str,
        metadata: dict[str, object],
        browser_profile_id: UUID | None = None,
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

    async def list(
        self,
        principal: Principal,
        limit: int,
        cursor: str | None,
    ) -> Page[BrowserProfileView]: ...

    async def revoke(self, principal: Principal, profile_id: UUID) -> BrowserProfileView: ...

    async def delete(self, principal: Principal, profile_id: UUID) -> None: ...

    async def begin_authentication(
        self,
        principal: Principal,
        profile_id: UUID,
        *,
        login_url: str,
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
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[BrowserGrantView]: ...

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


class DeviceService(Protocol):
    async def register(
        self,
        principal: Principal,
        registration: DeviceRegistration,
        idempotency_key: str | None = None,
    ) -> DeviceRegistrationResult: ...

    async def get(self, principal: Principal, device_id: UUID) -> DeviceView: ...

    async def list(
        self, principal: Principal, limit: int, cursor: str | None
    ) -> Page[DeviceView]: ...

    async def revoke(self, principal: Principal, device_id: UUID) -> DeviceView: ...

    async def delete(self, principal: Principal, device_id: UUID) -> None: ...

    async def enqueue_test_notification(
        self,
        principal: Principal,
        device_id: UUID,
        idempotency_key: str,
    ) -> TestNotificationResult: ...


class NotificationService(Protocol):
    async def list(
        self, principal: Principal, limit: int, cursor: str | None
    ) -> Page[NotificationInboxItem]: ...


class MemoryReadService(Protocol):
    async def list(
        self,
        principal: Principal,
        *,
        ceiling: Sensitivity,
        statuses: builtins.list[MemoryStatus] | None,
        belief_types: builtins.list[BeliefType] | None,
        subject: str | None,
        session_id: UUID | None,
        text: str | None,
        limit: int,
        cursor: str | None,
    ) -> Page[MemoryView]: ...

    async def get(
        self, principal: Principal, memory_id: UUID, *, ceiling: Sensitivity
    ) -> MemoryView: ...


class PersonaService(Protocol):
    async def get(self, principal: Principal) -> PersonaView: ...

    async def history(self, principal: Principal, *, limit: int) -> Page[PersonaView]: ...

    async def update(
        self,
        principal: Principal,
        *,
        expected_version: int,
        entries: builtins.list[PersonaEntryDraft],
    ) -> PersonaView: ...

    async def nominations(
        self,
        principal: Principal,
        *,
        state: PersonaNominationState | None,
    ) -> Page[PersonaNominationView]: ...

    async def affirm(self, principal: Principal, nomination_id: UUID) -> PersonaView: ...

    async def decline(self, principal: Principal, nomination_id: UUID) -> PersonaNominationView: ...
