"""Policy vocabulary and deterministic decision inputs."""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TrustLevel(StrEnum):
    PLATFORM = "platform"
    TRUSTED_CONFIGURATION = "trusted_configuration"
    USER = "user"
    INTERNAL_TOOL = "internal_tool"
    EXTERNAL_UNTRUSTED = "external_untrusted"
    MEMORY = "memory"
    KNOWLEDGE = "knowledge"


class SideEffectClass(StrEnum):
    NONE = "none"
    WORKSPACE_READ = "workspace_read"
    WORKSPACE_WRITE = "workspace_write"
    NETWORK_READ = "network_read"
    CODE_EXECUTION = "code_execution"
    PACKAGE_INSTALL = "package_install"
    SANDBOX_NETWORK = "sandbox_network"
    EXTERNAL_MESSAGE = "external_message"
    EXTERNAL_WRITE = "external_write"
    EXTERNAL_DELETE = "external_delete"
    FINANCIAL = "financial"
    PUBLICATION = "publication"
    CREDENTIAL_ACCESS = "credential_access"
    HOST_ACCESS = "host_access"
    PRIVILEGED = "privileged"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IdempotencyClass(StrEnum):
    READ_ONLY = "read_only"
    IDEMPOTENT = "idempotent"
    CONDITIONALLY_IDEMPOTENT = "conditionally_idempotent"
    NON_IDEMPOTENT = "non_idempotent"


class ExecutionTarget(BaseModel):
    kind: str
    isolated: bool
    network_enabled: bool
    device_id: str | None = None
    server_id: str | None = None


class PolicyDecisionType(StrEnum):
    ALLOW = "allow"
    ALLOW_WITH_MODIFICATIONS = "allow_with_modifications"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class PolicyDecisionRank(IntEnum):
    ALLOW = 0
    ALLOW_WITH_MODIFICATIONS = 1
    REQUIRE_APPROVAL = 2
    DENY = 3


class ActionKind(StrEnum):
    TOOL_CALL = "tool_call"
    MEMORY_WRITE = "memory_write"
    SKILL_AUTHORING = "skill_authoring"
    ARTIFACT_EXPORT = "artifact_export"


class ProposedAction(BaseModel):
    kind: ActionKind
    action_id: UUID
    tenant_id: str
    session_id: UUID
    run_id: UUID
    step_number: int
    name: str
    version: str | None = None
    summary: str
    side_effect: SideEffectClass
    risk: RiskLevel
    idempotency: IdempotencyClass
    required_scopes: set[str] = Field(default_factory=set)
    arguments: dict[str, Any]
    normalized_arguments_hash: str
    argument_trust: dict[str, TrustLevel] = Field(default_factory=dict)
    origin_trust: TrustLevel
    target: ExecutionTarget
    evaluated_at: datetime


class PolicyDecision(BaseModel):
    decision: PolicyDecisionType
    reason_code: str
    explanation: str
    modified_arguments: dict[str, Any] | None = None
    policy_version: str


class PolicyRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    side_effect: SideEffectClass
    decision: PolicyDecisionType
    condition: str | None = None
    otherwise: PolicyDecisionType | None = None


class HardlineRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: str
    applies_to: tuple[SideEffectClass, ...]
    message_code: str
    near_miss: str
    pattern: str | None = None
    paths: tuple[str, ...] = ()
    cidrs: tuple[str, ...] = ()
    source: str | None = None


class LoadedRuleset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: str
    profile_name: str
    profile_sha256: str
    hardline_sha256: str
    rules: tuple[PolicyRule, ...]
    hardline: tuple[HardlineRule, ...]
    default_effect: PolicyDecisionType
    external_untrusted_requires_approval: bool = True
    self_approval_enabled: bool = True
    approval_expiry_seconds: tuple[tuple[RiskLevel, int], ...]


class PolicyProfileRecord(BaseModel):
    policy_version: str
    profile_name: str
    profile_sha256: str
    hardline_sha256: str
    rule_count: int
    loaded_at: datetime
    loaded_by: str
