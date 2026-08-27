"""Run state, budgets, checkpoints, and terminal outcomes."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, PositiveInt, model_validator

from agent_core.domain.messages import ConversationItem, ProviderPin
from agent_core.domain.policies import TrustLevel
from agent_core.domain.provenance import ElidedSpan
from agent_core.domain.skills import LoadedSkillBody
from agent_core.domain.tools import ToolSpec


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RunKind(StrEnum):
    INTERACTIVE = "interactive"
    SKILL_REVIEW = "skill_review"
    DELEGATED = "delegated"


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
    synthesis_reserve_steps: int = Field(default=0, ge=0)
    synthesis_reserve_model_calls: int = Field(default=0, ge=0)
    synthesis_reserve_cost: Decimal = Field(default=Decimal("0"), ge=0)

    @model_validator(mode="after")
    def synthesis_reserve_fits(self) -> RunLimits:
        """Keep final-synthesis headroom strictly inside each total limit."""

        if self.synthesis_reserve_steps > 0 and self.synthesis_reserve_steps >= self.max_steps:
            raise ValueError("the synthesis step reserve must be below max_steps")
        if (
            self.synthesis_reserve_model_calls > 0
            and self.synthesis_reserve_model_calls >= self.max_model_calls
        ):
            raise ValueError("the synthesis model-call reserve must be below max_model_calls")
        if self.max_cost is None:
            if self.synthesis_reserve_cost != 0:
                raise ValueError("a synthesis cost reserve requires max_cost")
        elif self.synthesis_reserve_cost > 0 and self.synthesis_reserve_cost >= self.max_cost:
            raise ValueError("the synthesis cost reserve must be below max_cost")
        return self


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
    tool_pins_initialized: bool = False
    pinned_tool_names: list[str] = Field(default_factory=list)
    pinned_tool_versions: dict[str, str] = Field(default_factory=dict)
    pinned_tool_specs: dict[str, ToolSpec] = Field(default_factory=dict)
    working_state: dict[str, Any] = Field(default_factory=dict)
    loaded_skills: list[LoadedSkillBody] = Field(default_factory=list)
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
    context_origin_trust: TrustLevel = TrustLevel.USER
    created_at: datetime

    @model_validator(mode="after")
    def pinned_tools_match(self) -> RunCheckpoint:
        names = set(self.pinned_tool_names)
        if not set(self.pinned_tool_versions) <= names or not set(self.pinned_tool_specs) <= names:
            raise ValueError("checkpoint pinned tool metadata does not match its pinned tool names")
        return self


class Run(BaseModel):
    id: UUID
    session_id: UUID
    parent_run_id: UUID | None = None
    kind: RunKind = RunKind.INTERACTIVE
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
