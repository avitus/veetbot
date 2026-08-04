"""Stable, bounded workspace directory listing."""

from __future__ import annotations

from typing import Any

from agent_core.domain.execution import WorkspaceProvenance
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import ToolExecutionContext, ToolFailureKind, ToolResult, ToolSpec
from agent_core.tools.workspace.common import failure, success, workspace_from

ENTRY_LIMIT = 1000
INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "maxLength": 4096, "default": ""},
        "recursive": {"type": "boolean", "default": False},
    },
    "required": [],
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "entries": {
            "type": "array",
            "maxItems": ENTRY_LIMIT,
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "kind": {"enum": ["file", "directory"]},
                    "byte_count": {"type": "integer"},
                },
                "required": ["path", "kind", "byte_count"],
                "additionalProperties": False,
            },
        },
        "truncated": {"type": "boolean"},
    },
    "required": ["path", "entries", "truncated"],
    "additionalProperties": False,
}


class WorkspaceListFilesTool:
    spec = ToolSpec(
        name="workspace.list_files",
        version="1.0.0",
        description="List paths in this run's disposable workspace without reading contents.",
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        side_effect=SideEffectClass.WORKSPACE_READ,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        required_scopes={"workspace.read"},
        timeout_seconds=10,
        maximum_output_bytes=262_144,
        allow_parallel=True,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        path = str(arguments.get("path", ""))
        workspace = workspace_from(context)
        try:
            found = await workspace.listdir(path, recursive=bool(arguments.get("recursive", False)))
        except FileNotFoundError:
            return failure(
                ToolFailureKind.NOT_FOUND,
                "tool.not_found.no_such_path",
                "workspace path does not exist",
            )
        except NotADirectoryError:
            return failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.invalid_arguments.not_a_directory",
                "workspace path is a file",
            )
        ordered = sorted(found, key=lambda item: str(item.path).encode("utf-8"))
        selected = ordered[:ENTRY_LIMIT]
        entries = [
            {"path": str(item.path), "kind": item.kind, "byte_count": item.size_bytes}
            for item in selected
        ]
        result = success(
            {"path": path, "entries": entries, "truncated": len(ordered) > ENTRY_LIMIT}
        )
        provenances = [await workspace.provenance(str(item.path)) for item in selected]
        result.output_trust = (
            TrustLevel.INTERNAL_TOOL
            if all(value is WorkspaceProvenance.TOOL_WRITTEN for value in provenances)
            else TrustLevel.EXTERNAL_UNTRUSTED
        )
        return result
