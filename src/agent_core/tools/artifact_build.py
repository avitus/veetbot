"""Stage, render and attach an exact offline package within one invocation."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any, cast

from agent_core.domain.argument_views import approval_argument_value
from agent_core.domain.errors import ExecutionUnavailable, ToolValidationError, WorkspaceEscape
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolResult,
    ToolSpec,
)
from agent_core.execution.manager import SandboxManager
from agent_core.tools.artifact_export import ArtifactExportTool, _bare_filename
from agent_core.tools.sandbox_run_command import INPUT_SCHEMA as COMMAND_SCHEMA
from agent_core.tools.sandbox_run_command import SandboxRunCommandTool
from agent_core.tools.validation import validate_and_normalize

MAX_INPUT_BYTES = 1_048_576
MAX_FILE_BYTES = 32 * 1024 * 1024
_PATH = {
    "type": "string",
    "minLength": 1,
    "maxLength": 1024,
    "pattern": r"^(?!/)(?!.*(?:^|/)\.{1,2}(?:/|$))(?!.*//)(?!.*[\\\u0000])[^/]+(?:/[^/]+)*$",
}
INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "files": {
            "type": "object",
            "minProperties": 1,
            "maxProperties": 32,
            "propertyNames": _PATH,
            "additionalProperties": {"type": "string", "maxLength": MAX_INPUT_BYTES},
        },
        "command": COMMAND_SCHEMA["properties"]["command"],
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 240, "default": 120},
        "outputs": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "items": {
                "type": "object",
                "properties": {
                    "path": _PATH,
                    "filename": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 255,
                        "pattern": r"^[^/\\\u0000]+$",
                    },
                    "media_type": {"type": "string", "minLength": 1, "maxLength": 255},
                },
                "required": ["path", "filename", "media_type"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["files", "command", "outputs"],
    "additionalProperties": False,
}


def _failure(
    detail: str,
    *,
    kind: ToolFailureKind = ToolFailureKind.INVALID_ARGUMENTS,
    code: str = "tool.arguments_invalid",
    artifacts: list[Any] | None = None,
    external_text: str | None = None,
) -> ToolResult:
    attached = artifacts or []
    report = {"complete": False, "attached": attached, "error": detail}
    return ToolResult(
        ok=False,
        content=[TextPart(text=json.dumps(report))],
        structured=report,
        artifacts=attached,
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        failure=ToolFailure(
            kind=kind,
            reason_code=code,
            detail=detail,
            retryable=False,
            external_text=external_text or json.dumps(report),
        ),
    )


class ArtifactBuildTool:
    spec = ToolSpec(
        name="artifact.build",
        version="1.0.0",
        description=(
            "Create offline files in one operation: supply all input files, argv and "
            "explicit outputs. Writes, rendering and attachments share one approval "
            "when required. Fresh sandbox, no network or tool calls. Read artifact.read_guide "
            "for examples. Changed or failed builds require a new invocation."
        ),
        input_schema=INPUT_SCHEMA,
        output_schema={"type": "object"},
        side_effect=SideEffectClass.CODE_EXECUTION,
        risk=RiskLevel.HIGH,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        required_scopes={"sandbox.execute", "workspace.write", "artifact.write"},
        timeout_seconds=300,
        maximum_output_bytes=32768,
        allow_parallel=False,
        target_kind="sandbox",
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )

    def __init__(self, sandbox_factory: Callable[[], SandboxManager]) -> None:
        # The composition root supplies a fresh manager with deny egress and
        # no parent environment. Never reuse the ordinary run workspace.
        self._sandbox_factory = sandbox_factory

    async def approval_view(
        self, arguments: dict[str, Any], *, tenant_id: str
    ) -> tuple[str, dict[str, Any]]:
        del tenant_id
        names = ", ".join(
            approval_argument_value(item["filename"]) for item in arguments["outputs"]
        )
        count = len(arguments["outputs"])
        label = "file" if count == 1 else "files"
        summary = f"Build and attach {count} offline {label} to this chat: {names}"
        return " ".join(summary.split())[:240], arguments

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        try:
            arguments, _, _ = validate_and_normalize(arguments, INPUT_SCHEMA)
            files = cast(dict[str, str], arguments["files"])
            outputs = cast(list[dict[str, str]], arguments["outputs"])
            if sum(len(body.encode("utf-8")) for body in files.values()) > MAX_INPUT_BYTES:
                return _failure("Input files exceed 1 MiB in total.")
            if any(not _bare_filename(item["filename"]) for item in outputs):
                return _failure("Output filenames must be bare names.")
            if len({item["path"] for item in outputs}) != len(outputs):
                return _failure("Output paths must be unique.")
            if len({item["filename"] for item in outputs}) != len(outputs):
                return _failure("Output filenames must be unique.")
        except (ToolValidationError, UnicodeError):
            return _failure("The offline build package is invalid.")

        manager = self._sandbox_factory()
        attached: list[Any] = []
        try:
            workspace = manager.for_run(context.tenant_id, context.run_id, context.lease_epoch)
            isolated_context = replace(context, workspace=workspace, bridge_dispatch=None)
            runner = SandboxRunCommandTool(manager, hard_ceiling_multiplier=1)
            command_arguments = {
                "command": arguments["command"],
                "timeout_seconds": arguments["timeout_seconds"],
            }
            # Validate every path and argv before provisioning or staging anything.
            runner._command(command_arguments, isolated_context)
            for path in [*files, *(item["path"] for item in outputs)]:
                workspace.resolve(path)
            for path, body in files.items():
                await workspace.write(path, body.encode("utf-8"))
            result = await runner.execute(command_arguments, isolated_context)
            if not result.ok:
                return result
            execution = result.structured or {}
            if (
                execution.get("exit_code") != 0
                or execution.get("timed_out")
                or execution.get("killed_by") is not None
            ):
                return _failure(
                    "The offline command failed; no files were attached.",
                    kind=ToolFailureKind.UPSTREAM_ERROR,
                    code="tool.execution_failed",
                    external_text=json.dumps(execution)[:12000],
                )
            exporter = ArtifactExportTool(max_artifact_bytes=MAX_FILE_BYTES)
            for output in outputs:
                try:
                    exported = await exporter.execute(output, isolated_context)
                except Exception:
                    # Cancellation remains a BaseException and must propagate.
                    # Never expose storage diagnostics or lose completed exports.
                    return _failure(
                        "An output could not be attached. Only the listed files were attached.",
                        kind=ToolFailureKind.INTERNAL,
                        code="tool.internal_error",
                        artifacts=attached,
                    )
                if not exported.ok:
                    return _failure(
                        "An output could not be attached. Only the listed files were attached.",
                        kind=ToolFailureKind.OUTPUT_INVALID,
                        code="tool.output_invalid",
                        artifacts=attached,
                    )
                attached.extend(exported.artifacts)
            report = {"complete": True, "attached": attached, "exit_code": 0}
            return ToolResult(
                ok=True,
                content=[TextPart(text=json.dumps(report))],
                structured=report,
                artifacts=attached,
                output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
            )
        except (
            ToolValidationError,
            WorkspaceEscape,
            FileNotFoundError,
            IsADirectoryError,
            NotADirectoryError,
        ):
            return _failure("A build path or command is invalid.", artifacts=attached)
        except ExecutionUnavailable:
            return _failure(
                "The offline build service is unavailable.",
                kind=ToolFailureKind.TRANSPORT,
                code="tool.server_unreachable",
                artifacts=attached,
            )
        finally:
            await manager.close()
