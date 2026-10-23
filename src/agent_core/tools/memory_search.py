"""Deliberate governed belief retrieval tool."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any

from agent_core.application.people_context import PeopleAwareMemoryRetriever, PeopleContextService
from agent_core.domain.errors import ToolValidationError
from agent_core.domain.memory import (
    BeliefType,
    RecallMoment,
    RecallProfile,
    RecallQuery,
    Sensitivity,
)
from agent_core.domain.messages import MemoryRecallUsage, TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.recall_execution import EXHAUSTED, RecallBudgetExceededError
from agent_core.domain.sessions import project_scope
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolResult,
    ToolSpec,
)
from agent_core.memory.retrieval import HybridMemoryRetriever, recalls_owned_resources
from agent_core.ports.persistence import UnitOfWorkFactory

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
        version="1.1.0",
        description=(
            "Search governed beliefs before asking the user to repeat personal context "
            "(home location, preferences, standing constraints). A missing snapshot or automatic "
            "recall fact may still be known. Query the needed fact (e.g. 'home location city "
            "residence') even if the question names another topic. Tenant, principal and "
            "sensitivity limits apply. Scope gates local memories unless their subject is "
            "explicit; it ranks contextual and portable memories."
        ),
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

    def __init__(
        self,
        retriever: HybridMemoryRetriever,
        *,
        model_planning: bool = False,
        factory: UnitOfWorkFactory | None = None,
        people_context: PeopleContextService | None = None,
        overview_enabled: bool = False,
        inline_output_bytes: int = 4096,
    ) -> None:
        self._retriever = retriever
        self._model_planning = model_planning
        self._factory = factory
        self._people_context = people_context
        self._inline_output_bytes = inline_output_bytes
        self._overview_enabled = overview_enabled
        if model_planning:
            schema = deepcopy(INPUT_SCHEMA)
            schema["required"] = ["text"]
            kinds = schema["properties"].pop("belief_types")
            kinds["description"] = "Optional relevance hints; never exclude other stored types."
            schema["properties"]["type_hints"] = kinds
            schema["properties"]["as_of"]["description"] = "Requested time when facts were true."
            schema["properties"]["known_at"] = {
                "type": "string",
                "format": "date-time",
                "description": "Requested cutoff for recorded evidence.",
            }
            self.spec = self.spec.model_copy(
                update={
                    "version": "2.1.0",
                    "input_schema": schema,
                    "description": (
                        "Recall context before asking again. Omit scope/dates unless requested; "
                        "scope defaults to this session. Type hints rank, never filter. Tenant, "
                        "principal, sensitivity and local-memory scope limits apply."
                    ),
                }
            )
            if overview_enabled:
                schema["properties"]["relationship_group"] = {
                    "type": "string",
                    "enum": ["family", "partner", "friend", "work", "other"],
                    "description": (
                        "Read a bounded page of direct owner relationships; "
                        "family includes partners."
                    ),
                }
                schema["properties"]["people_after"] = {
                    "type": "string",
                    "format": "uuid",
                    "description": "Continue a relationship page using its returned cursor.",
                }
                schema["dependentRequired"] = {"people_after": ["relationship_group"]}
                # The shared tool-definition allocation also serves browser sessions.
                # Keep the schema's constraints and put its guidance in one short description.
                for field in schema["properties"].values():
                    field.pop("description", None)
                self.spec = self.spec.model_copy(
                    update={
                        "version": "2.2.0",
                        "input_schema": schema,
                        "description": (
                            "Recall memory before asking again. Type hints only rank. "
                            "relationship_group pages owner relationships; "
                            "people_after continues a page."
                        ),
                    }
                )

    def query(
        self,
        arguments: dict[str, Any],
        *,
        tenant_id: str,
        principal_id: str,
        current_scope: str | None = None,
    ) -> RecallQuery:
        if self._model_planning and "belief_types" in arguments:
            raise ValueError("Planned memory.search accepts type_hints, not hard belief_types")
        scope = arguments.get("scope") if self._model_planning else arguments["scope"]
        if scope is None:
            scope = current_scope
        if scope is None:
            raise ValueError("Memory search requires a trusted session scope")
        return RecallQuery.model_validate(
            {
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "current_scope": scope,
                "text": arguments["text"],
                "candidate_selection": self._retriever.retrieval_profile.candidate_selection,
                "subjects": arguments.get("subjects", []),
                "include_owned_resources": recalls_owned_resources(
                    arguments["text"], deliberate=True
                ),
                "belief_types": [] if self._model_planning else arguments.get("belief_types", []),
                "structured_belief_types": arguments.get("type_hints", [])
                if self._model_planning
                else [],
                "as_of": arguments.get("as_of"),
                "known_at": arguments.get("known_at") if self._model_planning else None,
                "profile": RecallProfile.DEEP,
                "budget_tokens": 2_000,
                "budget_bytes": min(self._inline_output_bytes, self.spec.maximum_output_bytes),
                "relationship_group": arguments.get("relationship_group")
                if self._overview_enabled
                else None,
                "people_after": arguments.get("people_after") if self._overview_enabled else None,
                "max_items": 20,
                "min_score": 0.1,
                "sensitivity_ceiling": Sensitivity.RESTRICTED,
            }
        )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        if arguments.get("relationship_group") is not None and self._people_context is None:
            raise ToolValidationError("People relationship lookup is unavailable")
        scope: str | None = None
        if self._model_planning and "scope" not in arguments:
            if self._factory is None:
                raise ValueError("Memory search requires a trusted session scope")
            async with self._factory() as uow:
                session = await uow.sessions.get(context.session_id, context.principal)
            scope = project_scope(session.metadata)
        query = self.query(
            arguments,
            tenant_id=context.tenant_id,
            principal_id=context.principal.principal_id,
            current_scope=scope,
        )
        try:
            retriever = (
                PeopleAwareMemoryRetriever(self._people_context, context.principal)
                if self._model_planning and self._people_context is not None
                else self._retriever
            )
            result = await retriever.recall(
                query,
                session_id=context.session_id,
                run_id=context.run_id,
                # The turn is the run: a belief this search surfaced is part of
                # what the answer had in front of it, so usage feedback and the
                # user's recall trace must both see it.
                turn_id=context.run_id,
                moment=RecallMoment.IN_TURN.value,
            )
        except RecallBudgetExceededError:
            return ToolResult(
                ok=False,
                content=[TextPart(text=EXHAUSTED)],
                failure=ToolFailure(
                    kind=ToolFailureKind.PERMISSION,
                    reason_code="memory_recall_budget_exhausted",
                    detail=EXHAUSTED,
                    retryable=False,
                ),
                output_trust=TrustLevel.MEMORY,
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
            memory_recall=MemoryRecallUsage(
                trace_id=result.trace_id,
                tokens=result.tokens,
                items=len(result.items) + len(result.people),
                content_sha256=hashlib.sha256(result.rendered.encode()).hexdigest(),
                ranked_ids=None if result.stages is None else result.stages.ranked_ids[:20],
            ),
        )
