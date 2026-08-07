"""Approval-path fixture that records a simulated external write."""

from __future__ import annotations

from typing import Any

from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import ToolExecutionContext, ToolResult, ToolSpec
from agent_core.tools.workspace.common import checksum, success

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "destination": {"type": "string", "maxLength": 256},
        "content": {"type": "string", "maxLength": 4096},
    },
    "required": ["destination", "content"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "recorded": {"const": True},
        "destination": {"type": "string"},
        "byte_count": {"type": "integer"},
        "checksum": {"type": "string"},
    },
    "required": ["recorded", "destination", "byte_count", "checksum"],
    "additionalProperties": False,
}


class DemoExternalWriteTool:
    spec = ToolSpec(
        name="demo.external_write",
        version="1.0.0",
        description=(
            "Record a simulated external write after explicit approval; no service is called."
        ),
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        risk=RiskLevel.HIGH,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        required_scopes={"demo.write"},
        timeout_seconds=10,
        maximum_output_bytes=4096,
        allow_parallel=False,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        data = str(arguments["content"]).encode("utf-8")
        await context.mark_effect_sent()
        return success(
            {
                "recorded": True,
                "destination": str(arguments["destination"]),
                "byte_count": len(data),
                "checksum": checksum(data),
            }
        )
