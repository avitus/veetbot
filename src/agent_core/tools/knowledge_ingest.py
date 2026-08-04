"""USER-origin-only knowledge document ingestion tool."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from agent_core.domain.knowledge import (
    DocumentAuthority,
    KnowledgeIngestRequest,
    KnowledgeVisibility,
)
from agent_core.domain.memory import Sensitivity
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import ToolExecutionContext, ToolResult, ToolSpec
from agent_core.knowledge.service import KnowledgeService
from agent_core.ports.persistence import UnitOfWorkFactory

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "artifact_id": {"type": "string", "format": "uuid"},
        "title": {"type": "string", "minLength": 1, "maxLength": 1024},
        "visibility": {
            "type": "string",
            "enum": [item.value for item in KnowledgeVisibility],
        },
        "project_scope": {"type": "string", "minLength": 1, "maxLength": 256},
        "document_id": {"type": "string", "format": "uuid"},
        "doc_date": {"type": "string", "format": "date"},
        "authority": {"type": "string", "enum": [item.value for item in DocumentAuthority]},
        "sensitivity": {"type": "string", "enum": [item.value for item in Sensitivity]},
    },
    "required": ["artifact_id", "title", "visibility"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"document_id": {"type": "string"}, "version": {"type": "integer"}},
    "required": ["document_id", "version"],
    "additionalProperties": False,
}


class KnowledgeIngestTool:
    spec = ToolSpec(
        name="knowledge.ingest",
        version="1.0.0",
        description="Ingest an existing caller-owned text artifact into governed knowledge.",
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.MEDIUM,
        idempotency=IdempotencyClass.IDEMPOTENT,
        required_scopes={"knowledge.write"},
        timeout_seconds=30,
        maximum_output_bytes=4096,
        allow_parallel=False,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )

    def __init__(self, service: KnowledgeService, uow_factory: UnitOfWorkFactory) -> None:
        self._service = service
        self._uow_factory = uow_factory

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        artifact_id = UUID(str(arguments["artifact_id"]))
        async with self._uow_factory() as uow:
            source = await uow.artifacts.get(artifact_id, context.principal)
        request = KnowledgeIngestRequest(
            source=source,
            title=str(arguments["title"]),
            visibility=KnowledgeVisibility(str(arguments["visibility"])),
            project_scope=arguments.get("project_scope"),
            document_id=(
                None
                if arguments.get("document_id") is None
                else UUID(str(arguments["document_id"]))
            ),
            doc_date=(
                None
                if arguments.get("doc_date") is None
                else date.fromisoformat(str(arguments["doc_date"]))
            ),
            authority=DocumentAuthority(
                str(arguments.get("authority", DocumentAuthority.PRINCIPAL_SUPPLIED.value))
            ),
            sensitivity=Sensitivity(str(arguments.get("sensitivity", Sensitivity.INTERNAL.value))),
        )
        document = await self._service.ingest(request, origin_trust=context.origin_trust)
        structured = {"document_id": str(document.document_id), "version": document.version}
        return ToolResult(
            ok=True,
            content=[TextPart(text=f"Ingested knowledge document {document.document_id}.")],
            structured=structured,
        )
