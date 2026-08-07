"""Deliberate governed belief retrieval tool."""

from __future__ import annotations

from typing import Any

from agent_core.domain.memory import (
    BeliefType,
    RecallMoment,
    RecallProfile,
    RecallQuery,
    Sensitivity,
)
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import ToolExecutionContext, ToolResult, ToolSpec
from agent_core.memory.retrieval import HybridMemoryRetriever

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "minLength": 1, "maxLength": 8192},
        "scope": {"type": "string", "minLength": 1, "maxLength": 256},
        "subjects": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
        "belief_types": {
            "type": "array",
            "items": {"type": "string", "enum": [item.value for item in BeliefType]},
            "maxItems": 10,
        },
        "as_of": {"type": "string", "format": "date-time"},
    },
    "required": ["text", "scope"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "trace_id": {"type": "string"},
        "belief_ids": {"type": "array", "items": {"type": "string"}},
        "truncated": {"type": "boolean"},
    },
    "required": ["trace_id", "belief_ids", "truncated"],
    "additionalProperties": False,
}


class MemorySearchTool:
    spec = ToolSpec(
        name="memory.search",
        version="1.0.0",
        description="Search governed long-term beliefs within the caller's hard scope.",
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        required_scopes=set(),
        timeout_seconds=10,
        maximum_output_bytes=64 * 1024,
        allow_parallel=True,
        output_trust=TrustLevel.MEMORY,
    )

    def __init__(self, retriever: HybridMemoryRetriever) -> None:
        self._retriever = retriever

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        query = RecallQuery.model_validate(
            {
                "tenant_id": context.tenant_id,
                "principal_id": context.principal.principal_id,
                "current_scope": arguments["scope"],
                "text": arguments["text"],
                "subjects": arguments.get("subjects", []),
                "belief_types": arguments.get("belief_types", []),
                "as_of": arguments.get("as_of"),
                "profile": RecallProfile.DEEP,
                "budget_tokens": 2_000,
                "max_items": 20,
                "min_score": 0.1,
                "sensitivity_ceiling": Sensitivity.RESTRICTED,
            }
        )
        result = await self._retriever.recall(
            query,
            session_id=context.session_id,
            run_id=context.run_id,
            moment=RecallMoment.IN_TURN.value,
        )
        structured = {
            "trace_id": str(result.trace_id),
            "belief_ids": [str(item.belief_id) for item in result.items],
            "truncated": result.truncated,
        }
        return ToolResult(
            ok=True,
            content=[TextPart(text=result.rendered)],
            structured=structured,
            output_trust=TrustLevel.MEMORY,
        )
