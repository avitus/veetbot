"""Policy classification values shared by tools and the runtime."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


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
