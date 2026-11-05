"""Governed knowledge passage retrieval tool."""

from __future__ import annotations

from typing import Any

from agent_core.domain.knowledge import KnowledgeQuery
from agent_core.domain.memory import Sensitivity
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.sessions import project_scope
from agent_core.domain.tools import ToolExecutionContext, ToolResult, ToolSpec
from agent_core.knowledge.service import KnowledgeService
from agent_core.ports.persistence import UnitOfWorkFactory

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

    def __init__(
        self, service: KnowledgeService, *, factory: UnitOfWorkFactory | None = None
    ) -> None:
        """Search through the service; the factory supplies a session's scope."""

        self._service = service
        self._factory = factory

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        """Run one governed search and render its passages for the model."""

        scope: str | None = arguments.get("scope")
        if "scope" not in arguments and self._factory is not None:
            # ADR-0299: the catalog lists the session's project documents, so a
            # search that names no scope reads under that same scope, as
            # memory.search does, and reaches every document the catalog named.
            async with self._factory() as uow:
                session = await uow.sessions.get(context.session_id, context.principal)
            scope = project_scope(session.metadata)
        query = KnowledgeQuery.model_validate(
            {
                "tenant_id": context.tenant_id,
                "principal_id": context.principal.principal_id,
                "current_scope": scope,
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
