"""Repository and run-scoped identity ports used by Milestone 1."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.approvals import (
    ApprovalRequest,
    ApprovalResolutionOutcome,
    ApprovalResolutionType,
)
from agent_core.domain.messages import (
    ModelAttempt,
    ModelRequest,
    ModelTurn,
    ModelUsage,
    ProviderPin,
    ResolvedModel,
    StopReason,
)
from agent_core.domain.persistence import (
    IdempotencyRecord,
    ModelCallRecord,
    ModelErrorKind,
    SessionHistory,
    TrajectoryProjection,
    UsageRollup,
    WorkerLease,
)
from agent_core.domain.policies import PolicyProfileRecord
from agent_core.domain.runs import BudgetScope, Run, RunCheckpoint, RunStatus, RunUsage, Step
from agent_core.domain.sessions import Session
from agent_core.domain.tools import ToolInvocation, ToolInvocationStatus
from agent_core.domain.trajectory import ArtifactRef, ExportConsent, TrajectoryExport


class AgentRepository(Protocol):
    async def put(self, agent: AgentSpec) -> None: ...

    async def get_version(self, agent_id: UUID, agent_version: str) -> AgentSpec: ...

    async def latest_version(self, agent_id: UUID) -> AgentSpec: ...


class SessionRepository(Protocol):
    async def create(self, session: Session) -> None: ...

    async def get(self, session_id: UUID, principal: Principal) -> Session: ...


class RunRepository(Protocol):
    async def create(self, run: Run) -> None: ...

    async def get(self, run_id: UUID, principal: Principal) -> Run: ...

    async def request_cancellation(self, run_id: UUID, expected_status: RunStatus) -> Run: ...

    async def transition(
        self,
        run_id: UUID,
        expected_status: RunStatus,
        new_status: RunStatus,
        *,
        failure: object | None = None,
        final_message: str | None = None,
        lease: WorkerLease | None = None,
    ) -> Run: ...

    async def update_counters(self, run: Run, *, lease: WorkerLease | None = None) -> None: ...

    async def set_seed_event_sequence(self, run_id: UUID, sequence: int) -> None: ...

    async def set_provider_pin(self, run_id: UUID, pin: ProviderPin) -> None: ...


class ToolInvocationRepository(Protocol):
    async def create(
        self, invocation: ToolInvocation, *, lease: WorkerLease | None = None
    ) -> ToolInvocation: ...

    async def find_by_idempotency_key(
        self, run_id: UUID, idempotency_key: str
    ) -> ToolInvocation | None: ...

    async def transition(
        self,
        invocation_id: UUID,
        expected_status: ToolInvocationStatus,
        invocation: ToolInvocation,
        *,
        lease: WorkerLease | None = None,
    ) -> ToolInvocation: ...

    async def list_for_run(self, run_id: UUID, principal: Principal) -> list[ToolInvocation]: ...


class ApprovalRepository(Protocol):
    async def create(self, request: ApprovalRequest) -> ApprovalRequest: ...

    async def discard_pending(self, approval_id: UUID) -> None: ...

    async def get(self, approval_id: UUID, principal: Principal) -> ApprovalRequest: ...

    async def get_by_action(self, action_id: UUID) -> ApprovalRequest | None: ...

    async def record_revalidation(
        self, action_id: UUID, policy_version: str
    ) -> ApprovalRequest: ...

    async def list_pending(
        self,
        principal: Principal,
        run_id: UUID | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[ApprovalRequest]: ...

    async def resolve(
        self,
        approval_id: UUID,
        principal: Principal,
        resolution: ApprovalResolutionType,
        reason: str | None,
    ) -> ApprovalResolutionOutcome: ...

    async def expire_due(
        self, now: datetime, limit: int, *, tenant_id: str
    ) -> list[ApprovalRequest]: ...

    async def cancel_for_run(self, run_id: UUID) -> int: ...


class PolicyProfileRepository(Protocol):
    async def record(self, profile: PolicyProfileRecord) -> PolicyProfileRecord: ...

    async def get(self, policy_version: str) -> PolicyProfileRecord | None: ...


class PrincipalResolver(Protocol):
    async def for_run(self, run: Run) -> Principal: ...


class BudgetLedger(Protocol):
    def check(self, run: Run, scope: BudgetScope) -> None: ...

    async def record_model_usage(
        self,
        run: Run,
        usage: ModelUsage,
        *,
        step: Step,
        attempt: ModelAttempt | None = None,
        request: ModelRequest | None = None,
        resolved_model: ResolvedModel | None = None,
        model_turn: ModelTurn | None = None,
        registry_version: str | None = None,
        stop_reason: StopReason | None = None,
        error_kind: ModelErrorKind | None = None,
    ) -> None: ...

    async def record_tool_usage(self, run: Run, count: int, *, step: Step) -> None: ...

    async def refund_orchestration_turn(self, run: Run, *, step: Step) -> None: ...


class CheckpointRepository(Protocol):
    async def write(
        self,
        run_id: UUID,
        checkpoint: RunCheckpoint,
        *,
        full: bool,
        lease: WorkerLease | None = None,
    ) -> int: ...

    async def latest(self, run_id: UUID) -> RunCheckpoint | None: ...

    async def prune(self, run_id: UUID, *, terminal: bool) -> int: ...

    async def delete_nonterminal(self, run_id: UUID) -> int: ...


class IdempotencyRepository(Protocol):
    async def get(
        self, key: str, tenant_id: str, principal_id: str
    ) -> IdempotencyRecord | None: ...

    async def create(self, record: IdempotencyRecord) -> IdempotencyRecord: ...


class UsageRepository(Protocol):
    async def record_attempt(self, call: ModelCallRecord) -> None: ...

    async def run_usage(self, run_id: UUID) -> RunUsage: ...

    async def tenant_usage(
        self, tenant_id: str, *, since: datetime, until: datetime
    ) -> UsageRollup: ...


class SessionHistoryRepository(Protocol):
    async def catch_up(self, session_id: UUID) -> SessionHistory: ...

    async def rebuild(self, session_id: UUID) -> SessionHistory: ...

    async def read(
        self, session_id: UUID, through_sequence: int | None = None
    ) -> SessionHistory: ...


class TrajectoryProjectionRepository(Protocol):
    async def catch_up(self, run_id: UUID) -> TrajectoryProjection | None: ...

    async def rebuild(self, run_id: UUID) -> TrajectoryProjection | None: ...

    async def read(self, run_id: UUID) -> TrajectoryProjection | None: ...


class ExportConsentRepository(Protocol):
    async def get(self, tenant_id: str, principal_id: str) -> ExportConsent | None: ...

    async def get_for_update(self, tenant_id: str, principal_id: str) -> ExportConsent | None: ...

    async def grant(self, consent: ExportConsent) -> ExportConsent: ...

    async def withdraw(
        self, tenant_id: str, principal_id: str, withdrawn_at: datetime
    ) -> ExportConsent: ...


class TrajectoryExportRepository(Protocol):
    async def get_for_run(self, run_id: UUID) -> TrajectoryExport | None: ...

    async def create(self, export: TrajectoryExport) -> TrajectoryExport: ...

    async def expire_for_principal(
        self, tenant_id: str, principal_id: str, expired_at: datetime
    ) -> int: ...

    async def list_expired(self, now: datetime, *, limit: int) -> list[ArtifactRef]: ...

    async def delete_expired(self, artifact_id: UUID, *, now: datetime) -> bool: ...


class MaintenanceRepository(Protocol):
    async def projection_sessions(self, limit: int) -> list[UUID]: ...

    async def trajectory_runs(self, limit: int) -> list[UUID]: ...

    async def checkpoint_runs(self, limit: int) -> list[tuple[UUID, bool]]: ...
