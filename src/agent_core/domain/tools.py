"""Tool declarations, invocation state, and pipeline outcomes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from agent_core.domain.messages import ContentPart, ToolResultItem
from agent_core.domain.policies import (
    ExecutionTarget,
    IdempotencyClass,
    PolicyDecision,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)

if TYPE_CHECKING:
    from agent_core.domain.agents import Principal


class ToolKind(StrEnum):
    CAPABILITY = "capability"
    CONTROL = "control"


class ToolSource(StrEnum):
    BUILTIN = "builtin"
    MCP = "mcp"
    DEVICE = "device"
    SANDBOX = "sandbox"


class ToolSpec(BaseModel):
    name: str
    version: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    side_effect: SideEffectClass
    risk: RiskLevel
    idempotency: IdempotencyClass
    required_scopes: set[str] = Field(default_factory=set)
    timeout_seconds: int
    maximum_output_bytes: int
    allow_parallel: bool
    kind: ToolKind = ToolKind.CAPABILITY
    target_kind: str = "in_process"
    output_trust: TrustLevel
    source: ToolSource = ToolSource.BUILTIN
    server_id: str | None = None
    device_id: str | None = None
    deprecated: bool = False


class ToolFailureKind(StrEnum):
    INVALID_ARGUMENTS = "invalid_arguments"
    NOT_FOUND = "not_found"
    PERMISSION = "permission"
    TIMEOUT = "timeout"
    OUTPUT_TOO_LARGE = "output_too_large"
    OUTPUT_INVALID = "output_invalid"
    UPSTREAM_ERROR = "upstream_error"
    TRANSPORT = "transport"
    OUTCOME_UNKNOWN = "outcome_unknown"
    INTERNAL = "internal"


class ToolFailure(BaseModel):
    kind: ToolFailureKind
    reason_code: str
    detail: str
    retryable: bool
    external_text: str | None = None


class ToolResult(BaseModel):
    ok: bool
    content: list[ContentPart]
    structured: dict[str, Any] | None = None
    artifacts: list[Any] = Field(default_factory=list)
    failure: ToolFailure | None = None
    output_trust: TrustLevel | None = None
    metrics: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def success_and_failure_are_consistent(self) -> ToolResult:
        if self.ok == (self.failure is not None):
            raise ValueError("successful results have no failure; failed results require one")
        return self


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    invocation_id: UUID
    call_id: str
    run_id: UUID
    session_id: UUID
    tenant_id: str
    principal: Principal
    step_number: int
    attempt_number: int
    lease_epoch: int
    idempotency_key: str
    deadline_at: datetime
    timeout_seconds: float
    maximum_output_bytes: int
    target: ExecutionTarget
    workspace: object | None
    artifacts: object
    credentials: object
    bridge_dispatch: object | None
    working_state: dict[str, Any]
    # Kept structural here to preserve the domain-to-ports dependency boundary;
    # runtime construction supplies an object satisfying CancellationToken.
    cancellation: object
    mark_effect_sent: Callable[[], Awaitable[None]]
    loaded_skills: tuple[dict[str, Any], ...] = ()
    available_tools: frozenset[str] = frozenset()
    origin_trust: TrustLevel = TrustLevel.EXTERNAL_UNTRUSTED
    argument_trust: dict[str, TrustLevel] = field(default_factory=dict)
    run_kind: str = "interactive"


class ToolOutcomeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"
    UNCERTAIN = "uncertain"


class ToolOutcome(BaseModel):
    status: ToolOutcomeStatus
    action: str
    reason_code: str
    message: str
    retryable: bool
    remediation: Literal["none", "modify_arguments", "request_approval"]


class ToolInvocationStatus(StrEnum):
    PROPOSED = "PROPOSED"
    AUTHORIZED = "AUTHORIZED"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DENIED = "DENIED"
    UNCERTAIN = "UNCERTAIN"


ALLOWED_TOOL_TRANSITIONS: dict[ToolInvocationStatus, frozenset[ToolInvocationStatus]] = {
    ToolInvocationStatus.PROPOSED: frozenset(
        {
            ToolInvocationStatus.AUTHORIZED,
            ToolInvocationStatus.WAITING_FOR_APPROVAL,
            ToolInvocationStatus.DENIED,
        }
    ),
    ToolInvocationStatus.AUTHORIZED: frozenset(
        {ToolInvocationStatus.RUNNING, ToolInvocationStatus.WAITING_FOR_APPROVAL}
    ),
    ToolInvocationStatus.WAITING_FOR_APPROVAL: frozenset(
        {ToolInvocationStatus.AUTHORIZED, ToolInvocationStatus.DENIED}
    ),
    ToolInvocationStatus.RUNNING: frozenset(
        {
            ToolInvocationStatus.RUNNING,
            ToolInvocationStatus.SUCCEEDED,
            ToolInvocationStatus.FAILED,
            ToolInvocationStatus.UNCERTAIN,
        }
    ),
    ToolInvocationStatus.SUCCEEDED: frozenset(),
    ToolInvocationStatus.FAILED: frozenset(),
    ToolInvocationStatus.DENIED: frozenset(),
    ToolInvocationStatus.UNCERTAIN: frozenset(),
}


class ToolInvocation(BaseModel):
    id: UUID
    run_id: UUID
    session_id: UUID
    step_number: int
    call_id: str
    tool_name: str
    tool_version: str
    tool_source: ToolSource = ToolSource.BUILTIN
    server_id: str | None = None
    idempotency_class: IdempotencyClass = IdempotencyClass.READ_ONLY
    side_effect: SideEffectClass
    risk: RiskLevel
    attempt_number: int = 1
    status: ToolInvocationStatus
    raw_arguments: str
    normalized_arguments: dict[str, Any] | None = None
    normalized_arguments_hash: str | None = None
    effective_arguments_hash: str | None = None
    idempotency_key: str
    effect_sent_at: datetime | None = None
    suspended_kind: str | None = None
    suspended_ref: str | None = None
    output_bytes: int | None = None
    truncated: bool = False
    artifact_id: UUID | None = None
    origin_trust: TrustLevel = TrustLevel.EXTERNAL_UNTRUSTED
    parallel_group: UUID | None = None
    outcome: ToolOutcome | None = None
    policy_decision: PolicyDecision | None = None
    structured_result: dict[str, Any] | None = None
    result_item: ToolResultItem | None = None
    created_at: datetime
    updated_at: datetime
