"""Delegation briefs, child outcomes, the ledger, and child-limit derivation."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from agent_core.domain.errors import DelegationValidationError
from agent_core.domain.runs import TERMINAL_RUN_STATUSES, Run, RunLimits, RunStatus, RunUsage

MAX_OBJECTIVE_CHARS = 4096
MAX_SUCCESS_CONDITION_CHARS = 2048
MAX_INLINE_CONTEXT_CHARS = 16384
MAX_CONTEXT_REFS = 8
MAX_ALLOWED_TOOLS = 16


class DelegationReturn(StrEnum):
    SUMMARY = "summary"
    SUMMARY_AND_ARTIFACTS = "summary_and_artifacts"


class DelegationRejectionReason(StrEnum):
    TOOLS_NOT_SUBSET = "delegation.tools_not_subset"
    DEPTH_EXCEEDED = "delegation.depth_exceeded"
    FANOUT_EXCEEDED = "delegation.fanout_exceeded"
    BUDGET_INSUFFICIENT = "delegation.budget_insufficient"
    TENANT_CAP = "delegation.tenant_cap"
    BRIEF_INVALID = "delegation.brief_invalid"


class DelegationLimits(BaseModel):
    """Requested child limits; every present value must be positive."""

    max_steps: int | None = Field(default=None, gt=0)
    max_model_calls: int | None = Field(default=None, gt=0)
    max_tool_calls: int | None = Field(default=None, gt=0)
    max_cost: Decimal | None = Field(default=None, gt=0)
    wall_seconds: int | None = Field(default=None, gt=0)


class DelegationBrief(BaseModel):
    """Everything one child needs to stop correctly; the child seeds from nothing else."""

    objective: str = Field(min_length=1, max_length=MAX_OBJECTIVE_CHARS)
    success_condition: str = Field(min_length=1, max_length=MAX_SUCCESS_CONDITION_CHARS)
    context: str | None = Field(default=None, max_length=MAX_INLINE_CONTEXT_CHARS)
    context_refs: list[UUID] = Field(default_factory=list, max_length=MAX_CONTEXT_REFS)
    allowed_tools: list[str] = Field(min_length=1, max_length=MAX_ALLOWED_TOOLS)
    limits: DelegationLimits | None = None


class DelegationRequest(BaseModel):
    """The delegate.run input: ordered briefs, one child each, and a return shape."""

    briefs: list[DelegationBrief] = Field(min_length=1)
    return_shape: DelegationReturn = DelegationReturn.SUMMARY


class ChildOutcome(BaseModel):
    child_run_id: UUID
    child_session_id: UUID
    status: RunStatus
    summary: str | None = None
    artifact_refs: list[UUID] = Field(default_factory=list)
    usage: RunUsage = Field(default_factory=RunUsage)
    failure_reason: str | None = None

    @field_validator("status")
    @classmethod
    def status_is_terminal(cls, value: RunStatus) -> RunStatus:
        if value not in TERMINAL_RUN_STATUSES:
            raise ValueError("a child outcome carries a terminal run status")
        return value


class DelegationResult(BaseModel):
    delegation_id: UUID
    children: list[ChildOutcome]


class DelegationStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    JOINED = "JOINED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class DelegationChild(BaseModel):
    """One ledger child: its brief, derived authority, and terminal facts once known.

    The identifiers are cleared, not the row deleted, when the child session
    alone is erased; ``links_erased_at`` on the ledger row records when.
    """

    index: int = Field(ge=0)
    brief: DelegationBrief
    derived_limits: RunLimits
    granted_scopes: frozenset[str]
    child_run_id: UUID | None = None
    child_session_id: UUID | None = None
    status: RunStatus | None = None
    summary: str | None = None
    artifact_refs: list[UUID] = Field(default_factory=list)
    usage: RunUsage | None = None
    failure_reason: str | None = None

    @field_validator("status")
    @classmethod
    def status_is_terminal(cls, value: RunStatus | None) -> RunStatus | None:
        if value is not None and value not in TERMINAL_RUN_STATUSES:
            raise ValueError("a recorded child status is a terminal run status")
        return value


class Delegation(BaseModel):
    """The parent's content-free view of one delegation: the separate trace."""

    id: UUID
    tenant_id: str
    principal_id: str
    parent_run_id: UUID
    parent_session_id: UUID
    invocation_id: UUID
    depth: int = Field(ge=0)
    request: DelegationRequest
    status: DelegationStatus
    children: list[DelegationChild]
    result: DelegationResult | None = None
    links_erased_at: datetime | None = None
    created_at: datetime
    joined_at: datetime | None = None


class DelegationDefaults(BaseModel):
    """Default child limits from the ``delegation:`` block; every value positive."""

    max_steps: int = Field(gt=0)
    max_model_calls: int = Field(gt=0)
    max_tool_calls: int = Field(gt=0)
    max_cost: Decimal = Field(gt=0)
    wall_seconds: int = Field(gt=0)


def _insufficient(dimension: str) -> DelegationValidationError:
    return DelegationValidationError(
        DelegationRejectionReason.BUDGET_INSUFFICIENT.value,
        f"the parent has no remaining {dimension} to delegate",
    )


def derive_child_limits(
    parent: Run,
    briefs: Sequence[DelegationBrief],
    defaults: DelegationDefaults,
    *,
    now: datetime,
) -> list[RunLimits]:
    """Derive each child's own limits, in brief order, bounded by the parent.

    Every derived value is the minimum of the requested or default value and
    the parent's remaining value; the children's ``max_cost`` values reserve
    the parent's remaining cost in order; the deadline is never later than the
    parent's. A parent with nothing remaining in any dimension cannot delegate.
    """

    remaining_steps = parent.limits.max_steps - parent.step_count
    remaining_model_calls = parent.limits.max_model_calls - parent.model_call_count
    remaining_tool_calls = parent.limits.max_tool_calls - parent.tool_call_count
    if remaining_steps <= 0:
        raise _insufficient("steps")
    if remaining_model_calls <= 0:
        raise _insufficient("model calls")
    if remaining_tool_calls <= 0:
        raise _insufficient("tool calls")
    remaining_cost: Decimal | None = None
    if parent.limits.max_cost is not None:
        remaining_cost = parent.limits.max_cost - parent.usage.cost
        if remaining_cost <= 0:
            raise _insufficient("cost")
    if parent.deadline_at is not None and parent.deadline_at <= now:
        raise _insufficient("wall time")

    derived: list[RunLimits] = []
    reserved = Decimal("0")
    for brief in briefs:
        requested = brief.limits or DelegationLimits()
        requested_cost = requested.max_cost or defaults.max_cost
        if remaining_cost is None:
            child_cost = requested_cost
        else:
            available = remaining_cost - reserved
            if available <= 0:
                raise _insufficient("cost")
            child_cost = min(requested_cost, available)
        reserved += child_cost
        wall_seconds = requested.wall_seconds or defaults.wall_seconds
        deadline = now + timedelta(seconds=wall_seconds)
        if parent.deadline_at is not None:
            deadline = min(parent.deadline_at, deadline)
        derived.append(
            RunLimits(
                max_steps=min(requested.max_steps or defaults.max_steps, remaining_steps),
                max_model_calls=min(
                    requested.max_model_calls or defaults.max_model_calls,
                    remaining_model_calls,
                ),
                max_tool_calls=min(
                    requested.max_tool_calls or defaults.max_tool_calls,
                    remaining_tool_calls,
                ),
                max_input_tokens=parent.limits.max_input_tokens,
                max_output_tokens=parent.limits.max_output_tokens,
                max_cost=child_cost,
                deadline_at=deadline,
            )
        )
    return derived
