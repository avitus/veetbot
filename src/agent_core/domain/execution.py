"""Execution-boundary values that never expose an execution-service host path."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from uuid import UUID


class ChangeKind(StrEnum):
    CREATED = "CREATED"
    MODIFIED = "MODIFIED"
    DELETED = "DELETED"


class WorkspaceProvenance(StrEnum):
    UNKNOWN = "unknown"
    TOOL_WRITTEN = "tool_written"
    SANDBOX_WRITTEN = "sandbox_written"


class KillReason(StrEnum):
    TIMEOUT = "TIMEOUT"
    MEMORY = "MEMORY"
    OUTPUT_LIMIT = "OUTPUT_LIMIT"
    PIDS = "PIDS"
    DISK = "DISK"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    SERVICE_SHUTDOWN = "SERVICE_SHUTDOWN"


class EgressMode(StrEnum):
    DENY = "deny"
    ALLOWLIST = "allowlist"


@dataclass(frozen=True, slots=True)
class EgressDestination:
    host: str
    ports: frozenset[int]


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    mode: EgressMode = EgressMode.DENY
    destinations: tuple[EgressDestination, ...] = ()


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    cpu_millicores: int
    memory_bytes: int
    pids_max: int
    workspace_bytes: int
    inodes_max: int
    wall_clock_seconds: int

    def __post_init__(self) -> None:
        if (
            min(
                self.cpu_millicores,
                self.memory_bytes,
                self.pids_max,
                self.workspace_bytes,
                self.inodes_max,
                self.wall_clock_seconds,
            )
            <= 0
        ):
            raise ValueError("every sandbox resource limit must be positive")


@dataclass(frozen=True, slots=True)
class BridgeEndpoint:
    socket_path: PurePosixPath
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class EnvironmentSpec:
    tenant_id: str
    run_id: UUID
    lease_epoch: int
    image_digest: str
    limits: ResourceLimits
    egress: EgressPolicy
    environment: Mapping[str, str]
    bridge: BridgeEndpoint | None = None


@dataclass(frozen=True, slots=True)
class EnvironmentHandle:
    environment_id: str
    tenant_id: str
    run_id: UUID
    lease_epoch: int
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionCommand:
    argv: Sequence[str]
    working_directory: PurePosixPath
    timeout_seconds: int
    stdin: bytes | None
    maximum_output_bytes: int


@dataclass(frozen=True, slots=True)
class FileChange:
    path: PurePosixPath
    change: ChangeKind
    size_bytes: int
    sha256: str | None


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    killed_by: KillReason | None
    files_changed: Sequence[FileChange]
    duration_ms: int


@dataclass(frozen=True, slots=True)
class WorkspaceEntry:
    path: PurePosixPath
    kind: str
    size_bytes: int
