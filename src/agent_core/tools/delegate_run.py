"""Suspending control tool that delegates bounded child runs."""

from __future__ import annotations

from typing import Any

from agent_core.domain.policies import (
    IdempotencyClass,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.tools import ToolExecutionContext, ToolKind, ToolResult, ToolSpec

DELEGATE_RUN_TOOL_NAME = "delegate.run"

_BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "objective": {"type": "string", "minLength": 1, "maxLength": 4096},
        "success_condition": {"type": "string", "minLength": 1, "maxLength": 2048},
        "context": {"type": ["string", "null"], "maxLength": 16384},
        "context_refs": {
            "type": "array",
            "items": {"type": "string", "format": "uuid"},
            "maxItems": 8,
        },
        "allowed_tools": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 96},
            "minItems": 1,
            "maxItems": 16,
        },
        "limits": {
            "type": ["object", "null"],
            "properties": {
                "max_steps": {"type": ["integer", "null"], "minimum": 1},
                "max_model_calls": {"type": ["integer", "null"], "minimum": 1},
                "max_tool_calls": {"type": ["integer", "null"], "minimum": 1},
                "max_cost": {"type": ["number", "string", "null"]},
                "wall_seconds": {"type": ["integer", "null"], "minimum": 1},
            },
            "additionalProperties": False,
        },
    },
    "required": ["objective", "success_condition", "allowed_tools"],
    "additionalProperties": False,
}


class DelegateRunTool:
    spec = ToolSpec(
        name=DELEGATE_RUN_TOOL_NAME,
        version="1.0.0",
        description=(
            "Delegate independent, bounded child runs — one per brief — and "
            "suspend this run until every child finishes."
        ),
        input_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "briefs": {"type": "array", "minItems": 1, "items": _BRIEF_SCHEMA},
                "return_shape": {
                    "type": "string",
                    "enum": ["summary", "summary_and_artifacts"],
                    "default": "summary",
                },
            },
            "required": ["briefs"],
            "additionalProperties": False,
        },
        output_schema=None,
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.IDEMPOTENT,
        required_scopes={"run.delegate"},
        timeout_seconds=30,
        maximum_output_bytes=256 * 1024,
        allow_parallel=False,
        kind=ToolKind.CONTROL,
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        del arguments, context
        raise RuntimeError("delegate.run is executed by the runtime control path")
