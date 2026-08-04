"""Run state, budgets, checkpoints, and terminal outcomes."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, PositiveInt, model_validator

from agent_core.domain.messages import ConversationItem, ProviderPin
from agent_core.domain.provenance import ElidedSpan


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class CancelReason(StrEnum):
    REQUESTED = "requested"
    DEADLINE = "deadline"
    FENCED = "fenced"


class BudgetScope(StrEnum):
    ADMISSION = "admission"
    STEP = "step"
    ATTEMPT = "attempt"


TERMINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})


class RunLimits(BaseModel):
    max_steps: int = 12
    max_model_calls: int = 12
    max_tool_calls: int = 24
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost: Decimal | None = None
    deadline_at: datetime | None = None


class RunUsage(BaseModel):
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int | None = None
    model_calls: int = 0
    tool_calls: int = 0
    cost: Decimal = Decimal("0")


class FailureReason(StrEnum):
    MAX_ATTEMPTS_EXCEEDED = "max_attempts_exceeded"
    BUDGET_EXCEEDED = "budget_exceeded"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    MAX_STEPS_EXCEEDED = "max_steps_exceeded"
    TOOL_LOOP_DETECTED = "tool_loop_detected"
    REPEATED_DENIAL = "repeated_denial"
    APPROVAL_EXPIRED = "approval_expired"
    INPUT_DEADLINE_EXCEEDED = "input_deadline_exceeded"
    CONTEXT_OVERFLOW = "context_overflow"
    MODEL_PERMANENT_ERROR = "model_permanent_error"
    EMPTY_MODEL_TURN = "empty_model_turn"
    AUTHORIZATION_ERROR = "authorization_error"
    CHILD_RUN_FAILED = "child_run_failed"
    INTERNAL_ERROR = "internal_error"


class RunFailure(BaseModel):
    reason: FailureReason
    error_class: str
    message: str
    step_number: int | None = None
    attempt_number: int | None = None
    occurred_at: datetime
    details: dict[str, Any] = Field(default_factory=dict)


class Step(BaseModel):
    run_id: UUID
    step_number: int
    started_at: datetime
    attempt_count: int = 0
    tool_call_count: int = 0
    compactions: int = 0


class ProviderContinuation(BaseModel):
    provider: str
    previous_response_id: str | None = None
    opaque_items: list[dict[str, Any]] = Field(default_factory=list)
    valid_for_provider_only: bool = True


class RunCheckpoint(BaseModel):
    run_id: UUID
    version: int
    status: RunStatus
    conversation: list[ConversationItem] = Field(default_factory=list)
    pending_tool_calls: list[Any] = Field(default_factory=list)
    pending_approval_ids: list[UUID] = Field(default_factory=list)
    working_state: dict[str, Any] = Field(default_factory=dict)
    compacted_summary: str | None = None
    summary_source_event_ids: list[PositiveInt] = Field(default_factory=list)
    summary_elided: list[ElidedSpan] = Field(default_factory=list)
    replaced_through_sequence: int = Field(default=0, ge=0)
    summary_depth: int = Field(default=0, ge=0, le=2)
    compactor_version: str | None = None
    budget_state: dict[str, Any] = Field(default_factory=dict)
    last_event_sequence: int = 0
    provider_continuation: ProviderContinuation | None = None
    provider_pin: ProviderPin | None = None
    created_at: datetime


class Run(BaseModel):
    id: UUID
    session_id: UUID
    parent_run_id: UUID | None = None
    tenant_id: str
    principal_scopes: set[str] = Field(default_factory=set)
    agent_id: UUID
    agent_version: str
    status: RunStatus
    step_count: int = 0
    model_call_count: int = 0
    tool_call_count: int = 0
    limits: RunLimits = Field(default_factory=RunLimits)
    usage: RunUsage = Field(default_factory=RunUsage)
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    lease_epoch: int = 0
    attempts: int = 0
    priority: int = 0
    scheduled_for: datetime | None = None
    deadline_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    failure: RunFailure | None = None
    final_message: str | None = None
    export_consent: bool = False
    provider_pin: ProviderPin | None = None
    seed_event_sequence: int = 0
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def deadline_matches_limits(self) -> Run:
        if self.deadline_at != self.limits.deadline_at:
            raise ValueError("run.deadline_at must equal run.limits.deadline_at")
        return self


class OutcomeKind(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUSPENDED = "suspended"
    FENCED = "fenced"


class RunOutcome(BaseModel):
    kind: OutcomeKind
    final_message: Any | None = None
    failure: RunFailure | None = None
    suspension: Any | None = None
