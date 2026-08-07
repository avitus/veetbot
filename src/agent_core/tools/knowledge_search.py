"""Governed knowledge passage retrieval tool."""

from __future__ import annotations

from typing import Any

from agent_core.domain.knowledge import KnowledgeQuery
from agent_core.domain.memory import Sensitivity
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import ToolExecutionContext, ToolResult, ToolSpec
from agent_core.knowledge.service import KnowledgeService

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "minLength": 1, "maxLength": 8192},
        "scope": {"type": "string", "minLength": 1, "maxLength": 256},
        "as_of": {"type": "string", "format": "date-time"},
    },
    "required": ["text"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "trace_id": {"type": "string"},
        "chunk_ids": {"type": "array", "items": {"type": "string"}},
        "truncated": {"type": "boolean"},
    },
    "required": ["trace_id", "chunk_ids", "truncated"],
    "additionalProperties": False,
}


class KnowledgeSearchTool:
    spec = ToolSpec(
        name="knowledge.search",
        version="1.0.0",
        description="Search visible knowledge passages with citations and a hard budget.",
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        required_scopes=set(),
        timeout_seconds=10,
        maximum_output_bytes=64 * 1024,
        allow_parallel=True,
        output_trust=TrustLevel.KNOWLEDGE,
    )

    def __init__(self, service: KnowledgeService) -> None:
        self._service = service

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        query = KnowledgeQuery.model_validate(
            {
                "tenant_id": context.tenant_id,
                "principal_id": context.principal.principal_id,
                "current_scope": arguments.get("scope"),
                "text": arguments["text"],
                "as_of": arguments.get("as_of"),
                "budget_tokens": 3_000,
                "max_passages": 10,
                "max_per_document": 2,
                "min_score": 0.1,
                "sensitivity_ceiling": Sensitivity.RESTRICTED,
            }
        )
        result = await self._service.search(
            query,
            session_id=context.session_id,
            run_id=context.run_id,
        )
        structured = {
            "trace_id": str(result.trace_id),
            "chunk_ids": [item.chunk_id for item in result.passages],
            "truncated": result.truncated,
        }
        return ToolResult(
            ok=True,
            content=[TextPart(text=result.rendered)],
            structured=structured,
            output_trust=TrustLevel.KNOWLEDGE,
        )
