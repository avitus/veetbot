"""Explicit episodic-history escalation tool."""

from __future__ import annotations

import json
from typing import Any

from agent_core.domain.memory import EpisodeQuery
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import ToolExecutionContext, ToolResult, ToolSpec
from agent_core.memory.retrieval import EventEpisodeSearch

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "minLength": 1, "maxLength": 8192},
        "since": {"type": "string", "format": "date-time"},
        "until": {"type": "string", "format": "date-time"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "events": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["events"],
    "additionalProperties": False,
}


class MemoryRecallEpisodesTool:
    spec = ToolSpec(
        name="memory.recall_episodes",
        version="1.0.0",
        description="Search the current session's episodic history explicitly.",
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

    def __init__(self, episodes: EventEpisodeSearch) -> None:
        self._episodes = episodes

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        query = EpisodeQuery.model_validate(
            {
                "tenant_id": context.tenant_id,
                "principal_id": context.principal.principal_id,
                "session_id": context.session_id,
                **arguments,
            }
        )
        events = await self._episodes.search(query)
        values = [
            {
                "sequence": item.sequence,
                "event_type": item.event_type,
                "created_at": item.created_at.isoformat(),
                "payload": item.payload,
            }
            for item in events
        ]
        return ToolResult(
            ok=True,
            content=[TextPart(text=json.dumps(values, ensure_ascii=False, sort_keys=True))],
            structured={"events": values},
            output_trust=TrustLevel.MEMORY,
        )
