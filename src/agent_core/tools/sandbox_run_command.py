"""The sole builtin that asks the isolated execution service to run code."""

from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import PurePosixPath
from typing import Any, cast

from agent_core.domain.errors import ToolValidationError, WorkspaceEscape
from agent_core.domain.execution import ExecutionCommand
from agent_core.domain.messages import ContentPart, TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolResult,
    ToolSpec,
)
from agent_core.execution.manager import SandboxManager
from agent_core.ports.execution import WorkspaceHandle
from agent_core.tools.bridge import BridgeDispatch, ProgrammaticBridgeSession

_MAX_VECTOR_BYTES = 64 * 1024

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {
            "type": "array",
            "minItems": 1,
            "maxItems": 1024,
            "items": {"type": "string"},
        },
        "working_directory": {"type": "string", "maxLength": 4096, "default": ""},
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
    },
    "required": ["command"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "exit_code": {"type": ["integer", "null"]},
        "stdout": {"type": "string"},
        "stderr": {"type": "string"},
        "timed_out": {"type": "boolean"},
        "killed_by": {"type": ["string", "null"]},
        "duration_ms": {"type": "integer"},
        "stdout_truncated": {"type": "boolean"},
        "stderr_truncated": {"type": "boolean"},
        "files_changed": {"type": "array", "items": {"type": "object"}},
    },
    "required": [
        "exit_code",
        "stdout",
        "stderr",
        "timed_out",
        "killed_by",
        "duration_ms",
        "stdout_truncated",
        "stderr_truncated",
        "files_changed",
    ],
    "additionalProperties": False,
}


class SandboxRunCommandTool:
    spec = ToolSpec(
        name="sandbox.run_command",
        version="1.0.0",
        description=(
            "Run an argument vector in an isolated disposable workspace. Network is off by "
            "default. Export files worth keeping; the workspace does not survive interruption. "
            "Large output is truncated and retained as an artifact."
        ),
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        side_effect=SideEffectClass.CODE_EXECUTION,
        risk=RiskLevel.HIGH,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        required_scopes={"sandbox.execute"},
        timeout_seconds=300,
        maximum_output_bytes=1_048_576,
        allow_parallel=False,
        target_kind="sandbox",
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )

    def __init__(self, manager: SandboxManager) -> None:
        self._manager = manager

    @staticmethod
    def _command(arguments: dict[str, Any], context: ToolExecutionContext) -> ExecutionCommand:
        raw = arguments.get("command")
        if not isinstance(raw, list) or not raw or any(not isinstance(item, str) for item in raw):
            raise ToolValidationError("command must be a non-empty argument vector of strings")
        command = tuple(raw)
        if (
            any("\x00" in item for item in command)
            or sum(len(item.encode("utf-8")) for item in command) > _MAX_VECTOR_BYTES
        ):
            raise ToolValidationError("command argument vector is invalid or exceeds 64 KiB")
        executable = command[0]
        if "/" in executable and not executable.startswith("/"):
            raise ToolValidationError(
                "command[0] must be absolute or a bare executable name; use a shell for ./ files"
            )
        working = str(arguments.get("working_directory", ""))
        if working == ".":
            working = ""
        if context.workspace is None:
            raise RuntimeError("sandbox command has no workspace")
        workspace = cast(WorkspaceHandle, context.workspace)
        try:
            resolved = workspace.resolve(working)
        except WorkspaceEscape as exc:
            raise ToolValidationError("working_directory escapes /workspace") from exc
        relative = resolved.relative_to(workspace.root)
        requested = int(arguments.get("timeout_seconds", context.timeout_seconds))
        return ExecutionCommand(
            argv=command,
            working_directory=PurePosixPath(relative),
            timeout_seconds=min(requested, int(context.timeout_seconds)),
            stdin=None,
            maximum_output_bytes=context.maximum_output_bytes * 4,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        try:
            command = self._command(arguments, context)
            bridge = None
            if context.bridge_dispatch is not None:
                source = json.dumps(
                    {
                        "argv": list(command.argv),
                        "working_directory": str(command.working_directory),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                bridge = ProgrammaticBridgeSession(
                    script_hash=hashlib.sha256(source).hexdigest(),
                    token=secrets.token_urlsafe(32),
                    dispatch=cast(BridgeDispatch, context.bridge_dispatch),
                )
            result = await self._manager.execute_for(
                context.tenant_id,
                context.run_id,
                context.lease_epoch,
                command,
                bridge=bridge,
            )
        except ToolValidationError as exc:
            return ToolResult(
                ok=False,
                content=[],
                failure=ToolFailure(
                    kind=ToolFailureKind.INVALID_ARGUMENTS,
                    reason_code="tool.arguments_invalid",
                    detail=str(exc),
                    retryable=False,
                ),
            )
        structured = {
            "exit_code": result.exit_code,
            "stdout": result.stdout.decode("utf-8", errors="replace"),
            "stderr": result.stderr.decode("utf-8", errors="replace"),
            "timed_out": result.timed_out,
            "killed_by": None if result.killed_by is None else result.killed_by.value,
            "duration_ms": result.duration_ms,
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
            "files_changed": [
                {
                    "path": str(change.path),
                    "change": change.change.value,
                    "size_bytes": change.size_bytes,
                    "sha256": change.sha256,
                }
                for change in result.files_changed
            ],
        }
        content: list[ContentPart] = [TextPart(text=str(structured["stdout"]))]
        if structured["stderr"]:
            content.append(TextPart(text=str(structured["stderr"])))
        return ToolResult(
            ok=True,
            content=content,
            structured=structured,
            output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        )
