"""Give the owner one file: text the model wrote, or a file from the run's workspace."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, cast

from agent_core.domain.errors import (
    ArtifactIntegrityError,
    WorkspaceEscape,
    WorkspaceReadLimitExceededError,
)
from agent_core.domain.messages import TextPart
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
# The same bound workspace.write_text puts on text the model writes.
MAX_CONTENT_BYTES = 1_048_576
# Text the owner can open anywhere; nothing a viewer would execute (ADR-0122).
CONTENT_MEDIA_TYPES = frozenset({"text/plain", "text/markdown", "text/csv", "application/json"})

LEGACY_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1, "maxLength": 4096},
        "filename": {"type": "string", "minLength": 1, "maxLength": 1024},
        "media_type": {"type": "string", "minLength": 1, "maxLength": 255},
    },
    "required": ["path", "filename", "media_type"],
    "additionalProperties": False,
}

# `path` defaults to the empty string, exactly as workspace.list_files does, so
# the shipped `path_inside_workspace` policy condition admits a content-only
# call without a change to the policy profile. The tool, not the schema,
# requires exactly one of a non-empty path and content: a top-level oneOf
# cannot tell a defaulted path from a supplied one.
INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "maxLength": 4096, "default": ""},
        "content": {"type": "string", "maxLength": MAX_CONTENT_BYTES},
        "filename": {"type": "string", "minLength": 1, "maxLength": 1024},
        "media_type": {"type": "string", "minLength": 1, "maxLength": 255},
    },
    "required": ["filename", "media_type"],
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

_DESCRIPTION = (
    "Give the user a file; it is attached to your reply automatically. Pass `content` "
    "to create a text file (text/plain, text/markdown, text/csv, or application/json) "
    "from text you write, or `path` to export a file this run created in its workspace. "
    "This is the only way a file reaches the user: the workspace is discarded when the "
    "run finishes or pauses. Never say a file is attached unless this call succeeded."
)


def _failure(kind: ToolFailureKind, reason_code: str, detail: str) -> ToolResult:
    return ToolResult(
        ok=False,
        content=[],
        failure=ToolFailure(kind=kind, reason_code=reason_code, detail=detail, retryable=False),
    )


def _invalid(detail: str) -> ToolResult:
    return _failure(ToolFailureKind.INVALID_ARGUMENTS, "tool.arguments_invalid", detail)


def _unavailable() -> ToolResult:
    return _failure(
        ToolFailureKind.INTERNAL,
        "tool.internal_error",
        "artifact collaborators are unavailable",
    )


def _has(collaborator: object, method: str) -> bool:
    try:
        return callable(getattr(collaborator, method, None))
    except RuntimeError:
        return False


def _bare_filename(filename: str) -> bool:
    return (
        filename not in {".", ".."}
        and not any(separator in filename for separator in ("/", "\\", "\x00"))
        and filename.strip() == filename
    )


async def _single(data: bytes) -> AsyncIterator[bytes]:
    yield data


class ArtifactExportTool:
    spec = ToolSpec(
        name="artifact.export",
        version="2.0.0",
        description=_DESCRIPTION,
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
        path = str(arguments.get("path") or "")
        content = arguments.get("content")
        if bool(path) == (content is not None):
            return _invalid("exactly one of path or content is required")
        if content is not None:
            return await self._write_content(str(content), arguments, context)
        return await self._export_path(path, arguments, context)

    async def _write_content(
        self, content: str, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        filename = str(arguments["filename"])
        media_type = str(arguments["media_type"])
        if media_type not in CONTENT_MEDIA_TYPES:
            return _invalid("content supports only plain text, Markdown, CSV, and JSON")
        if not _bare_filename(filename):
            return _invalid("filename must be a bare file name")
        data = content.encode("utf-8")
        if len(data) > MAX_CONTENT_BYTES:
            return _invalid("content exceeds 1 MiB")
        if not _has(context.artifacts, "create"):
            return _unavailable()
        writer = cast(ArtifactWriter, context.artifacts)
        try:
            ref = await writer.create(
                _single(data), filename, media_type, TrustLevel.EXTERNAL_UNTRUSTED
            )
        except ArtifactIntegrityError as exc:
            return _failure(
                ToolFailureKind.OUTPUT_TOO_LARGE, "tool.output_invalid", type(exc).__name__
            )
        return _attached(ref.artifact_id, ref.sha256, ref.size_bytes, ref.media_type)

    async def _export_path(
        self, path: str, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        if not (_has(context.workspace, "stream") and _has(context.artifacts, "create")):
            return _unavailable()
        workspace = cast(WorkspaceHandle, context.workspace)
        writer = cast(ArtifactWriter, context.artifacts)
        try:
            stream = workspace.stream(path, _MAX_ARTIFACT_BYTES)
            try:
                ref = await writer.create(
                    stream,
                    str(arguments["filename"]),
                    str(arguments["media_type"]),
                    TrustLevel.EXTERNAL_UNTRUSTED,
                )
            finally:
                close_stream = getattr(stream, "aclose", None)
                if close_stream is not None:
                    await close_stream()
        except (
            FileNotFoundError,
            IsADirectoryError,
            NotADirectoryError,
            WorkspaceEscape,
        ) as exc:
            return _invalid(type(exc).__name__)
        except (ArtifactIntegrityError, WorkspaceReadLimitExceededError) as exc:
            return _failure(
                ToolFailureKind.OUTPUT_TOO_LARGE, "tool.output_invalid", type(exc).__name__
            )
        return _attached(ref.artifact_id, ref.sha256, ref.size_bytes, ref.media_type)


def _attached(artifact_id: object, sha256: str, size_bytes: int, media_type: str) -> ToolResult:
    structured = {
        "artifact_id": str(artifact_id),
        "sha256": sha256,
        "size_bytes": size_bytes,
        "media_type": media_type,
    }
    # The model sees only what the platform knows: the id and the stored size.
    # The file's name and type came from the model and are not echoed back as
    # tool output (ADR-0122).
    reported = {
        "artifact_id": str(artifact_id),
        "attached_to_reply": True,
        "size_bytes": size_bytes,
    }
    return ToolResult(
        ok=True,
        content=[TextPart(text=json.dumps(reported, sort_keys=True, separators=(",", ":")))],
        structured=structured,
        artifacts=[structured],
    )


class LegacyArtifactExportTool(ArtifactExportTool):
    """Compatibility registration for sessions pinned to the path-only 1.0.0 revision."""

    spec = ToolSpec(
        name="artifact.export",
        version="1.0.0",
        description="Export one file from this run's disposable workspace as a durable artifact.",
        input_schema=LEGACY_INPUT_SCHEMA,
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
