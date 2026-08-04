"""Idempotent UTF-8 workspace writer."""

from __future__ import annotations

from typing import Any

from agent_core.domain.errors import WorkspaceEscape, WorkspaceReadLimitExceededError
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import ToolExecutionContext, ToolFailureKind, ToolResult, ToolSpec
from agent_core.tools.workspace.common import checksum, failure, success, workspace_from

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "maxLength": 4096},
        "content": {"type": "string", "maxLength": 1_048_576},
    },
    "required": ["path", "content"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "byte_count": {"type": "integer"},
        "checksum": {"type": "string"},
        "created": {"type": "boolean"},
    },
    "required": ["path", "byte_count", "checksum", "created"],
    "additionalProperties": False,
}


class WorkspaceWriteTextTool:
    spec = ToolSpec(
        name="workspace.write_text",
        version="1.0.0",
        description=(
            "Write UTF-8 text inside this run's disposable workspace. "
            "The workspace does not survive an interruption; export files worth keeping."
        ),
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        side_effect=SideEffectClass.WORKSPACE_WRITE,
        risk=RiskLevel.MEDIUM,
        idempotency=IdempotencyClass.IDEMPOTENT,
        required_scopes={"workspace.write"},
        timeout_seconds=10,
        maximum_output_bytes=4096,
        allow_parallel=False,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        path = str(arguments["path"])
        data = str(arguments["content"]).encode("utf-8", errors="strict")
        workspace = workspace_from(context)
        try:
            await workspace.read_bounded(path, 0)
            created = False
        except WorkspaceReadLimitExceededError:
            created = False
        except FileNotFoundError:
            created = True
        except (WorkspaceEscape, IsADirectoryError):
            return failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.arguments_invalid",
                "workspace path is not a writable file",
            )
        try:
            await workspace.write(path, data)
        except (WorkspaceEscape, IsADirectoryError):
            return failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.arguments_invalid",
                "workspace path is not a writable file",
            )
        return success(
            {
                "path": path,
                "byte_count": len(data),
                "checksum": checksum(data),
                "created": created,
            }
        )
