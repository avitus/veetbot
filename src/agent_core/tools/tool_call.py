"""The control tool that reaches a deferred tool (ADR-0123).

A deferred tool is pinned for the session and listed in the deferred tool
index, but its full definition is not sent to the provider. The model calls it
through `tool.call`. The tool pipeline unwraps the call before resolution, so
validation, classification, policy, approval, idempotency and events all run
under the deferred tool's own name; this tool is never executed itself.
"""

from __future__ import annotations

from typing import Any

from agent_core.domain.policies import (
    IdempotencyClass,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.tools import ToolExecutionContext, ToolKind, ToolResult, ToolSpec

TOOL_CALL_TOOL_NAME = "tool.call"
TOOL_CALL_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 256},
        "arguments": {"type": "object", "default": {}},
    },
    "required": ["name"],
    "additionalProperties": False,
}


class ToolCallTool:
    spec = ToolSpec(
        name=TOOL_CALL_TOOL_NAME,
        version="1.0.0",
        description=(
            "Call a tool listed in the deferred tool index. Pass its exact name and "
            "an arguments object. If the arguments are invalid, the result carries "
            "the tool's input schema."
        ),
        input_schema=TOOL_CALL_INPUT_SCHEMA,
        output_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
        },
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.IDEMPOTENT,
        required_scopes=set(),
        timeout_seconds=1,
        maximum_output_bytes=16 * 1024,
        allow_parallel=False,
        kind=ToolKind.CONTROL,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        del arguments, context
        raise RuntimeError("tool.call is unwrapped by the tool pipeline and never executes")
