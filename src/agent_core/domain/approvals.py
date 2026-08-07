"""Durable approval requests and guarded resolution outcomes."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from agent_core.domain.policies import ActionKind, PolicyDecision, RiskLevel


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ApprovalResolutionType(StrEnum):
    APPROVE_ONCE = "approve_once"
    DENY = "deny"


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


class ApprovalResolutionOutcome(BaseModel):
    state: ApprovalResolutionState
    approval: ApprovalRequest


class ApprovalCursor(BaseModel):
    """Repository-level keyset cursor after API decoding."""

    created_at: datetime
    id: UUID
