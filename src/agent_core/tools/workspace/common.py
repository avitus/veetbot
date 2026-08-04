"""Shared workspace tool helpers with no host filesystem access."""

from __future__ import annotations

import hashlib
import json
from typing import Any, cast

from agent_core.domain.messages import TextPart
from agent_core.domain.tools import ToolExecutionContext, ToolFailure, ToolFailureKind, ToolResult
from agent_core.ports.execution import WorkspaceHandle


def workspace_from(context: ToolExecutionContext) -> WorkspaceHandle:
    if context.workspace is None:
        raise RuntimeError("workspace collaborator is unavailable")
    return cast(WorkspaceHandle, context.workspace)


def checksum(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def success(structured: dict[str, Any]) -> ToolResult:
    return ToolResult(
        ok=True,
        content=[TextPart(text=json.dumps(structured, sort_keys=True, separators=(",", ":")))],
        structured=structured,
    )


def failure(kind: ToolFailureKind, reason_code: str, detail: str) -> ToolResult:
    return ToolResult(
        ok=False,
        content=[],
        failure=ToolFailure(
            kind=kind,
            reason_code=reason_code,
            detail=detail,
            retryable=False,
        ),
    )
