"""Explicitly export one workspace file as a durable artifact."""

from __future__ import annotations

from typing import Any, cast

from agent_core.domain.errors import WorkspaceEscape, WorkspaceReadLimitExceededError
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolResult,
    ToolSpec,
)
from agent_core.ports.artifacts import ArtifactWriter
from agent_core.ports.execution import WorkspaceHandle

_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1, "maxLength": 4096},
        "filename": {"type": "string", "minLength": 1, "maxLength": 1024},
        "media_type": {"type": "string", "minLength": 1, "maxLength": 255},
    },
    "required": ["path", "filename", "media_type"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "artifact_id": {"type": "string"},
        "sha256": {"type": "string"},
        "size_bytes": {"type": "integer"},
        "media_type": {"type": "string"},
    },
    "required": ["artifact_id", "sha256", "size_bytes", "media_type"],
    "additionalProperties": False,
}


class ArtifactExportTool:
    spec = ToolSpec(
        name="artifact.export",
        version="1.0.0",
        description="Export one file from this run's disposable workspace as a durable artifact.",
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        side_effect=SideEffectClass.WORKSPACE_READ,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.IDEMPOTENT,
        required_scopes={"artifact.write"},
        timeout_seconds=30,
        maximum_output_bytes=4096,
        allow_parallel=False,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        raw_workspace = context.workspace
        raw_writer = context.artifacts
        try:
            available = callable(getattr(raw_workspace, "stream", None)) and callable(
                getattr(raw_writer, "create", None)
            )
        except RuntimeError:
            available = False
        if not available:
            return ToolResult(
                ok=False,
                content=[],
                failure=ToolFailure(
                    kind=ToolFailureKind.INTERNAL,
                    reason_code="tool.internal_error",
                    detail="artifact collaborators are unavailable",
                    retryable=False,
                ),
            )
        workspace = cast(WorkspaceHandle, raw_workspace)
        writer = cast(ArtifactWriter, raw_writer)
        try:
            ref = await writer.create(
                workspace.stream(str(arguments["path"]), _MAX_ARTIFACT_BYTES),
                str(arguments["filename"]),
                str(arguments["media_type"]),
                TrustLevel.EXTERNAL_UNTRUSTED,
            )
        except (
            FileNotFoundError,
            IsADirectoryError,
            NotADirectoryError,
            WorkspaceEscape,
        ) as exc:
            return ToolResult(
                ok=False,
                content=[],
                failure=ToolFailure(
                    kind=ToolFailureKind.INVALID_ARGUMENTS,
                    reason_code="tool.arguments_invalid",
                    detail=type(exc).__name__,
                    retryable=False,
                ),
            )
        except WorkspaceReadLimitExceededError as exc:
            return ToolResult(
                ok=False,
                content=[],
                failure=ToolFailure(
                    kind=ToolFailureKind.OUTPUT_TOO_LARGE,
                    reason_code="tool.output_invalid",
                    detail=type(exc).__name__,
                    retryable=False,
                ),
            )
        structured = {
            "artifact_id": str(ref.artifact_id),
            "sha256": ref.sha256,
            "size_bytes": ref.size_bytes,
            "media_type": ref.media_type,
        }
        return ToolResult(ok=True, content=[], structured=structured, artifacts=[structured])
