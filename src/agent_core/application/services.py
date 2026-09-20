"""Principal-explicit application service contracts shared by entry points."""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.browser import (
    BrowserActionKind,
    BrowserAuthenticationView,
    BrowserGrantView,
    BrowserProfileView,
)
from agent_core.domain.devices import DeviceInvocationStatus, DeviceRegistration
from agent_core.domain.email import EmailDraft, EmailDraftEdit, EmailLearningState, EmailOperation
from agent_core.domain.folders import FolderProposalState
from agent_core.domain.memory import BeliefType, MemoryStatus, Sensitivity
from agent_core.domain.people import (
    PeopleErasure,
    PeopleOperation,
    PeopleRelationshipFilter,
    Person,
)
from agent_core.domain.people_imports import (
    PeopleImportCancel,
    PeopleImportRequest,
    PeopleImportView,
)
from agent_core.domain.people_views import (
    CreatePerson,
    LegacyPeopleLinkResult,
    PeopleCorrectionRequest,
    PeopleCorrectionResult,
    PeopleErasureView,
    PeopleEvidenceView,
    PeopleForgetRequest,
    PeopleIdentityRequest,
    PeoplePage,
    PeopleSectionPage,
    PeopleSectionQuery,
    PersonProfile,
    UpdatePerson,
)
from agent_core.domain.persona import PersonaEntryDraft, PersonaNominationState
from agent_core.domain.schedules import (
    ScheduleDefinition,
    ScheduleDefinitionPatch,
    ScheduleOccurrence,
    ScheduleRecord,
    ScheduleState,
)
from agent_core.domain.surfaces import IssuedPairingCode, Pairing
from agent_core.domain.views import (
    ApprovalFilters,
    ApprovalView,
    ArtifactContent,
    ArtifactView,
    CancelResult,
    ContentBlock,
    DeviceIngestResult,
    DeviceInvocationResultView,
    DeviceInvocationView,
    DeviceRegistrationResult,
    DeviceView,
    FolderProposalView,
    FolderView,
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
        self,
        principal: Principal,
        limit: int,
        cursor: str | None,
        *,
        states: frozenset[ScheduleState] | None = None,
    ) -> Page[ScheduleRecord]: ...

    async def update(
        self,
        principal: Principal,
        schedule_id: UUID,
        expected_revision: int,
        definition: ScheduleDefinition,
    ) -> ScheduleRecord: ...

    async def patch(
        self,
        principal: Principal,
        schedule_id: UUID,
        expected_revision: int,
        patch: ScheduleDefinitionPatch,
        idempotency_key: str,
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

    async def list_pending_invocations(
        self,
        principal: Principal,
        device_id: UUID,
    ) -> builtins.list[DeviceInvocationView]: ...

    async def record_invocation_result(
        self,
        principal: Principal,
        device_id: UUID,
        invocation_id: UUID,
        status: DeviceInvocationStatus,
    ) -> DeviceInvocationResultView: ...


class DeviceIngestService(Protocol):
    async def ingest(
        self,
        principal: Principal,
        device_id: UUID,
        *,
        channel: str,
        sender: str,
        body: str,
        received_at: datetime,
    ) -> DeviceIngestResult: ...


class NotificationService(Protocol):
    async def list(
        self, principal: Principal, limit: int, cursor: str | None
    ) -> Page[NotificationInboxItem]: ...


class SurfaceService(Protocol):
    async def list(self, principal: Principal) -> builtins.list[DeviceView]: ...

    async def get(self, principal: Principal, surface_id: UUID) -> DeviceView: ...

    async def issue_code(
        self,
        principal: Principal,
        surface_id: UUID,
        *,
        granted_scopes: frozenset[str],
        label: str | None,
        idempotency_key: str,
    ) -> IssuedPairingCode: ...

    async def list_pairings(
        self, principal: Principal, surface_id: UUID
    ) -> builtins.list[Pairing]: ...

    async def revoke_pairing(self, principal: Principal, pairing_id: UUID) -> Pairing: ...

    async def delete_pairing(self, principal: Principal, pairing_id: UUID) -> None: ...


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


class PeopleErasureOperations(Protocol):
    """Operator recovery remains available when People capture is disabled."""

    async def export(
        self,
        principal: Principal,
        receipt_id: UUID,
    ) -> tuple[PeopleErasure, list[PeopleErasure]]: ...

    async def reapply(
        self,
        principal: Principal,
        receipt: PeopleErasure,
        parts: list[PeopleErasure],
        *,
        session_id: UUID,
    ) -> PeopleErasureView: ...

    async def get(
        self,
        principal: Principal,
        receipt_id: UUID,
        *,
        ceiling: Sensitivity,
    ) -> PeopleErasureView: ...


class PeopleService(Protocol):
    async def link_existing(
        self, principal: Principal, *, limit: int = 100, cursor: str | None = None
    ) -> LegacyPeopleLinkResult: ...
    async def list_imports(
        self,
        principal: Principal,
        *,
        ceiling: Sensitivity,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[PeopleImportView]: ...

    async def create_import(
        self, principal: Principal, request: PeopleImportRequest, *, key: str, ceiling: Sensitivity
    ) -> PeopleImportView: ...
    async def get_import(
        self, principal: Principal, job_id: UUID, *, ceiling: Sensitivity
    ) -> PeopleImportView: ...
    async def cancel_import(
        self,
        principal: Principal,
        job_id: UUID,
        request: PeopleImportCancel,
        *,
        ceiling: Sensitivity,
        key: str,
    ) -> PeopleImportView: ...

    async def forget(
        self,
        principal: Principal,
        person_id: UUID,
        request: PeopleForgetRequest,
        *,
        key: str,
        ceiling: Sensitivity,
    ) -> PeopleErasureView: ...

    async def correct(
        self,
        principal: Principal,
        person_id: UUID,
        request: PeopleCorrectionRequest,
        *,
        key: str,
        ceiling: Sensitivity,
    ) -> PeopleCorrectionResult: ...

    async def identity_operation(
        self,
        principal: Principal,
        request: PeopleIdentityRequest,
        *,
        key: str,
        ceiling: Sensitivity,
    ) -> PeopleOperation: ...
    async def operation(
        self, principal: Principal, operation_id: UUID, *, ceiling: Sensitivity
    ) -> PeopleOperation | PeopleErasureView: ...

    async def section(
        self,
        principal: Principal,
        person_id: UUID,
        request: PeopleSectionQuery,
        *,
        ceiling: Sensitivity,
    ) -> PeopleSectionPage: ...
    async def evidence(
        self, principal: Principal, person_id: UUID, reference: UUID, *, ceiling: Sensitivity
    ) -> PeopleEvidenceView: ...

    async def create(
        self, principal: Principal, request: CreatePerson, *, key: str, ceiling: Sensitivity
    ) -> Person: ...
    async def update(
        self,
        principal: Principal,
        person_id: UUID,
        request: UpdatePerson,
        *,
        key: str,
        ceiling: Sensitivity,
    ) -> Person: ...
    async def get(
        self, principal: Principal, person_id: UUID, *, ceiling: Sensitivity
    ) -> PersonProfile: ...
    async def list(
        self,
        principal: Principal,
        *,
        ceiling: Sensitivity,
        text: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        as_of: datetime | None = None,
        state: str | None = None,
        pinned: bool | None = None,
        sort: str = "id",
        relationship: PeopleRelationshipFilter | None = None,
    ) -> PeoplePage: ...


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


class FolderService(Protocol):
    async def create(self, principal: Principal, *, name: str) -> FolderView: ...

    async def list(self, principal: Principal) -> Page[FolderView]: ...

    async def get(self, principal: Principal, folder_id: UUID) -> FolderView: ...

    async def rename(self, principal: Principal, folder_id: UUID, *, name: str) -> FolderView: ...

    async def delete(self, principal: Principal, folder_id: UUID) -> None: ...

    async def move_session(
        self, principal: Principal, session_id: UUID, *, folder_id: UUID | None
    ) -> SessionView: ...

    async def proposals(
        self, principal: Principal, *, state: FolderProposalState | None
    ) -> Page[FolderProposalView]: ...

    async def accept(
        self, principal: Principal, proposal_id: UUID, *, name: str | None = None
    ) -> FolderProposalView: ...

    async def decline(self, principal: Principal, proposal_id: UUID) -> FolderProposalView: ...


class EmailSubscriptionService(Protocol):
    """The bulk-sender census and its three owner gestures (Milestone 30)."""

    async def browse(
        self,
        principal: Principal,
        *,
        account_id: str | None = None,
        state: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, object]: ...

    async def keep(
        self, principal: Principal, subscription_id: str, expected_revision: int, *, kept: bool
    ) -> dict[str, object]: ...

    async def unsubscribe(
        self,
        principal: Principal,
        targets: list[tuple[str, str, int]],
        *,
        archive_existing: bool,
        idempotency_key: str,
    ) -> EmailOperation: ...

    async def spam(
        self,
        principal: Principal,
        subscription_id: str,
        expected_revision: int,
        *,
        spam: bool,
        idempotency_key: str,
    ) -> EmailOperation: ...


class EmailService(Protocol):
    """Email projections and commands; entry points cannot reach repositories."""

    @property
    def subscriptions(self) -> EmailSubscriptionService: ...

    async def accounts(self, principal: Principal) -> dict[str, object]: ...

    async def threads(
        self,
        principal: Principal,
        *,
        view: Literal["priority", "other", "all"] = "priority",
        account_id: str | None = None,
        text: str | None = None,
        cursor: str | None = None,
        limit: int = 5,
    ) -> dict[str, object]: ...

    async def thread(self, principal: Principal, thread_id: UUID) -> dict[str, object]: ...

    async def feedback(
        self,
        principal: Principal,
        *,
        thread_id: UUID,
        target: Literal["thread", "person", "topic"],
        judgment: Literal["important", "less_important", "needs_reply", "no_reply_needed"],
        explanation: str | None = None,
        target_value: str | None = None,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object]: ...

    async def undo_feedback(self, principal: Principal, feedback_id: UUID) -> dict[str, object]: ...

    async def draft(self, principal: Principal, draft_id: UUID) -> EmailDraft: ...

    async def edit_draft(
        self,
        principal: Principal,
        draft_id: UUID,
        edit: EmailDraftEdit,
        *,
        idempotency_key: str | None = None,
    ) -> EmailDraft: ...

    async def draft_revisions(self, principal: Principal, draft_id: UUID) -> dict[str, object]: ...

    async def submit_task(
        self,
        principal: Principal,
        *,
        kind: Literal["refresh", "draft", "send"],
        thread_id: UUID | None = None,
        draft_id: UUID | None = None,
        expected_revision: int | None = None,
        instruction: str | None = None,
        idempotency_key: str | None = None,
    ) -> EmailOperation: ...

    async def operation(self, principal: Principal, operation_id: UUID) -> EmailOperation: ...

    async def archive(
        self,
        principal: Principal,
        thread_id: UUID,
        expected_revision: int,
        *,
        archived: bool,
        idempotency_key: str,
    ) -> EmailOperation: ...

    async def learning(self, principal: Principal) -> EmailLearningState: ...

    async def pause_learning(self, principal: Principal, paused: bool) -> EmailLearningState: ...

    async def reset_learning(
        self, principal: Principal, scope: Literal["preferences", "style", "all"]
    ) -> EmailLearningState: ...

    async def discussion(self, principal: Principal, thread_id: UUID) -> dict[str, object]: ...

    async def dismiss(
        self,
        principal: Principal,
        thread_id: UUID,
        expected_revision: int,
        *,
        dismissed: bool = True,
    ) -> dict[str, object]:
        """Set or clear handled state, rejecting a stale expected thread revision."""
        ...

    async def discard_draft(
        self, principal: Principal, draft_id: UUID, expected_revision: int
    ) -> EmailDraft: ...

    async def exclude_source(
        self, principal: Principal, thread_id: UUID, expected_revision: int
    ) -> dict[str, object]: ...

    async def endorse_style(
        self, principal: Principal, draft_id: UUID, expected_revision: int
    ) -> EmailLearningState: ...


class CallingService(Protocol):
    async def list_calls(
        self, principal: Principal, *, limit: int = 10, cursor: str | None = None
    ) -> dict[str, Any]: ...

    async def get_call(self, principal: Principal, call_id: str) -> dict[str, Any]: ...

    async def stop(self, principal: Principal, call_id: str) -> dict[str, Any]: ...

    async def delete(self, principal: Principal, call_id: str) -> dict[str, Any]: ...


class CallIngressService(Protocol):
    async def receive(self, body: bytes, signature: str, secret: str) -> bool: ...
