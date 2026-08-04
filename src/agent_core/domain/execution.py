"""Execution-boundary value objects shared by workspace and sandbox ports."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath


class ChangeKind(StrEnum):
    CREATED = "CREATED"
    MODIFIED = "MODIFIED"
    DELETED = "DELETED"


class WorkspaceProvenance(StrEnum):
    UNKNOWN = "unknown"
    TOOL_WRITTEN = "tool_written"
    SANDBOX_WRITTEN = "sandbox_written"


@dataclass(frozen=True, slots=True)
class FileChange:
    path: PurePosixPath
    change: ChangeKind
    size_bytes: int
    sha256: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceEntry:
    path: PurePosixPath
    kind: str
    size_bytes: int
