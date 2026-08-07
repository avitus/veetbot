"""Strict UTF-8 workspace reader."""

from __future__ import annotations

import codecs
from typing import Any

from agent_core.domain.errors import WorkspaceEscape, WorkspaceReadLimitExceededError
from agent_core.domain.execution import WorkspaceProvenance
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import ToolExecutionContext, ToolFailureKind, ToolResult, ToolSpec
from agent_core.tools.workspace.common import checksum, failure, success, workspace_from

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"path": {"type": "string", "maxLength": 4096}},
    "required": ["path"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "content": {"type": "string"},
        "byte_count": {"type": "integer"},
        "checksum": {"type": "string"},
    },
    "required": ["path", "content", "byte_count", "checksum"],
    "additionalProperties": False,
}


class WorkspaceReadTextTool:
    spec = ToolSpec(
        name="workspace.read_text",
        version="1.0.0",
        description="Read a UTF-8 text file inside this run's disposable workspace.",
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        side_effect=SideEffectClass.WORKSPACE_READ,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        required_scopes={"workspace.read"},
        timeout_seconds=10,
        maximum_output_bytes=1_048_576,
        allow_parallel=True,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        path = str(arguments["path"])
        workspace = workspace_from(context)
        try:
            data = await workspace.read_bounded(path, self.spec.maximum_output_bytes)
        except WorkspaceEscape:
            return failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.arguments_invalid",
                "workspace path failed containment validation",
            )
        except WorkspaceReadLimitExceededError:
            return failure(
                ToolFailureKind.OUTPUT_TOO_LARGE,
                "tool.output_invalid",
                "workspace file exceeds the declared output limit",
            )
        except FileNotFoundError:
            return failure(
                ToolFailureKind.NOT_FOUND,
                "tool.not_found.no_such_path",
                "workspace path does not exist",
            )
        except IsADirectoryError:
            return failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.invalid_arguments.not_a_file",
                "workspace path is a directory",
            )
        if b"\x00" in data:
            return failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.invalid_arguments.not_text",
                "workspace path contains binary data",
            )
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        try:
            pieces = [
                decoder.decode(data[index : index + 4096]) for index in range(0, len(data), 4096)
            ]
            pieces.append(decoder.decode(b"", final=True))
        except UnicodeDecodeError:
            return failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.invalid_arguments.not_text",
                "workspace path is not strict UTF-8",
            )
        structured = {
            "path": path,
            "content": "".join(pieces),
            "byte_count": len(data),
            "checksum": checksum(data),
        }
        result = success(structured)
        provenance = await workspace.provenance(path)
        result.output_trust = (
            TrustLevel.INTERNAL_TOOL
            if provenance is WorkspaceProvenance.TOOL_WRITTEN
            else TrustLevel.EXTERNAL_UNTRUSTED
        )
        return result
