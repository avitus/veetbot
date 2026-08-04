"""Suspending control tool for mid-run user input."""

from __future__ import annotations

from typing import Any

from agent_core.domain.policies import (
    IdempotencyClass,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.tools import ToolExecutionContext, ToolKind, ToolResult, ToolSpec


class AskUserTool:
    spec = ToolSpec(
        name="conversation.ask_user",
        version="1.0.0",
        description="Pause this run and ask the user one concise question.",
        input_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"question": {"type": "string", "minLength": 1, "maxLength": 4096}},
            "required": ["question"],
            "additionalProperties": False,
        },
        output_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "question_id": {"type": "string"},
                "answered": {"type": "boolean"},
            },
            "required": ["question_id", "answered"],
            "additionalProperties": False,
        },
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.IDEMPOTENT,
        required_scopes=set(),
        timeout_seconds=1,
        maximum_output_bytes=16 * 1024,
        allow_parallel=False,
        kind=ToolKind.CONTROL,
        output_trust=TrustLevel.USER,
    )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        del arguments, context
        raise RuntimeError("conversation.ask_user is executed by the runtime control path")
