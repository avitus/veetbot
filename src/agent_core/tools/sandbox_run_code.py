"""Code mode: run a model-written Python script whose tool calls cross the bridge.

ADR-0342. The script runs in the run's sandbox, never on the worker. Each tool
call it makes re-enters the ordinary pipeline through the bridge.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import PurePosixPath
from typing import Any, cast

from agent_core.domain.argument_views import approval_argument_value
from agent_core.domain.errors import ExecutionUnavailable
from agent_core.domain.execution import SANDBOX_RUN_CODE_TOOL_NAME, ExecutionCommand
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
from agent_core.tools.bridge import BridgeDispatch, ProgrammaticBridgeSession

_MAX_SOURCE_BYTES = 64 * 1024
_SUMMARY_PREFIX = "Run a Python script in Veetbot's sandbox: "
_SUMMARY_LIMIT = 160

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "minLength": 1, "maxLength": _MAX_SOURCE_BYTES},
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
    },
    "required": ["code"],
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
        "tool_calls": {"type": "integer"},
        "tool_call_limit_reached": {"type": "boolean"},
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
        "tool_calls",
        "tool_call_limit_reached",
    ],
    "additionalProperties": False,
}


class SandboxRunCodeTool:
    spec = ToolSpec(
        name=SANDBOX_RUN_CODE_TOOL_NAME,
        version="1.0.0",
        description=(
            "Run Python in the isolated sandbox for loops, paging and joins. For public "
            "research use web.search and web.fetch directly. Call tools.<name>(**arguments) "
            "or tools.call(name, arguments) for checked tool results (text or parsed JSON). "
            "Refusals raise ToolCallError(status, reason_code). Print what you need: only "
            "printed output comes back. At most 64 tool calls per script; network is off."
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

    def __init__(self, manager: SandboxManager, *, hard_ceiling_multiplier: int = 4) -> None:
        if hard_ceiling_multiplier < 1:
            raise ValueError("hard ceiling multiplier must be positive")
        self._manager = manager
        self._hard_ceiling_multiplier = hard_ceiling_multiplier

    async def approval_view(
        self, arguments: dict[str, Any], *, tenant_id: str
    ) -> tuple[str, dict[str, Any]]:
        """The card names the script's first line, masked like the arguments view."""
        del tenant_id
        code = str(arguments.get("code", ""))
        first = next((line.strip() for line in code.splitlines() if line.strip()), "")
        summary = _SUMMARY_PREFIX + " ".join(str(approval_argument_value(first)).split())
        if len(summary) > _SUMMARY_LIMIT:
            summary = summary[: _SUMMARY_LIMIT - 1] + "…"
        return summary, arguments

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        code = arguments.get("code")
        if (
            not isinstance(code, str)
            or not code.strip()
            or "\x00" in code
            or len(code.encode("utf-8")) > _MAX_SOURCE_BYTES
        ):
            return ToolResult(
                ok=False,
                content=[],
                failure=ToolFailure(
                    kind=ToolFailureKind.INVALID_ARGUMENTS,
                    reason_code="tool.arguments_invalid",
                    detail="code must be Python source of 1 byte to 64 KiB without NUL",
                    retryable=False,
                ),
            )
        requested = int(arguments.get("timeout_seconds", context.timeout_seconds))
        command = ExecutionCommand(
            argv=("python", "-m", "agent_core.execution.code_runner"),
            working_directory=PurePosixPath("."),
            timeout_seconds=min(requested, int(context.timeout_seconds)),
            stdin=code.encode("utf-8"),
            maximum_output_bytes=context.maximum_output_bytes * self._hard_ceiling_multiplier,
        )
        bridge = None
        if context.bridge_dispatch is not None:
            # The hash covers the invocation, so a replay reads recorded outcomes
            # and the same source submitted later executes for real.
            identity = json.dumps(
                {"code": code, "outer_call_id": str(context.call_id)},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            bridge = ProgrammaticBridgeSession(
                script_hash=hashlib.sha256(identity).hexdigest(),
                token=secrets.token_urlsafe(32),
                dispatch=cast(BridgeDispatch, context.bridge_dispatch),
            )
        try:
            result = await self._manager.execute_for(
                context.tenant_id,
                context.run_id,
                context.lease_epoch,
                command,
                bridge=bridge,
            )
        except ExecutionUnavailable:
            return ToolResult(
                ok=False,
                content=[],
                failure=ToolFailure(
                    kind=ToolFailureKind.TRANSPORT,
                    reason_code="tool.server_unreachable",
                    detail="sandbox execution service is unavailable",
                    retryable=True,
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
            "tool_calls": 0 if bridge is None else bridge.call_count,
            "tool_call_limit_reached": bridge is not None and bridge.limit_exceeded,
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
