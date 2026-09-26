"""Durable approval requests and guarded resolution outcomes."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from agent_core.domain.browser_task_grants import BrowserTaskGrantOffer, TaskGrantNotCovered
from agent_core.domain.policies import ActionKind, PolicyDecision, RiskLevel


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ApprovalResolutionType(StrEnum):
    APPROVE_ONCE = "approve_once"
    # ADR-0129: approve the pending browser action once and create a
    # session-bound task grant from the approval's offer.
    APPROVE_FOR_TASK = "approve_for_task"
    DENY = "deny"


# Resolutions that approve the pending action. Every other resolution denies.
APPROVING_RESOLUTIONS = frozenset(
    {ApprovalResolutionType.APPROVE_ONCE, ApprovalResolutionType.APPROVE_FOR_TASK}
)


def approval_status_for(resolution: ApprovalResolutionType) -> ApprovalStatus:
    """The stored status a resolution produces (ADR-0129: both approvals approve)."""

    return ApprovalStatus.APPROVED if resolution in APPROVING_RESOLUTIONS else ApprovalStatus.DENIED


def approval_resolution_document(
    resolution: ApprovalResolutionType, reason: str | None, task_grant_id: UUID | None
) -> dict[str, Any]:
    """The persisted resolution record; only a task approval names its grant."""

    document: dict[str, Any] = {"resolution": resolution.value, "reason": reason}
    if task_grant_id is not None:
        document["task_grant_id"] = str(task_grant_id)
    return document


class ApprovalResolutionState(StrEnum):
    APPLIED = "applied"
    ALREADY_RESOLVED_IDENTICALLY = "already_resolved_identically"
    ALREADY_RESOLVED_DIFFERENTLY = "already_resolved_differently"


class ApprovalRequest(BaseModel):
    id: UUID
    tenant_id: str
    principal_id: str
    session_id: UUID
    run_id: UUID
    action_kind: ActionKind
    action_id: UUID
    tool_invocation_id: UUID | None = None
    status: ApprovalStatus
    action_summary: str
    tool_name: str | None = None
    arguments: dict[str, Any]
    # SHA-256 of each argument the view truncated for length, so a client holding
    # the original can still verify it exactly. Absent for short and for
    # sensitivity-redacted values. Older records carry none.
    argument_digests: dict[str, str] = Field(default_factory=dict)
    normalized_arguments_hash: str
    required_scopes: set[str]
    agent_version: str
    risk: RiskLevel
    policy_reason: str
    policy_decision: PolicyDecision
    policy_version: str
    revalidated_policy_version: str | None = None
    resolution: ApprovalResolutionType | None = None
    resolution_reason: str | None = None
    expires_at: datetime | None = None
    created_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    # ADR-0129: the server-authored task-grant offer shown on a browser.act
    # card, why the session's active grant did not cover the action, and the
    # grant an approve_for_task resolution created. Older records carry none.
    task_grant_offer: BrowserTaskGrantOffer | None = None
    task_grant_not_covered: TaskGrantNotCovered | None = None
    task_grant_id: UUID | None = None


class ApprovalResolutionOutcome(BaseModel):
    state: ApprovalResolutionState
    approval: ApprovalRequest


class ApprovalCursor(BaseModel):
    """Repository-level keyset cursor after API decoding."""

    created_at: datetime
    id: UUID
