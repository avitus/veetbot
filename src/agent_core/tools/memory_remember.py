"""Explicit, provenance-bound persistent memory formation tool."""

from __future__ import annotations

from typing import Any

from agent_core.domain.memory import BeliefType, Portability, Sensitivity
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import ToolExecutionContext, ToolResult, ToolSpec
from agent_core.memory.formation import GovernedMemoryService

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "statement": {"type": "string", "minLength": 1, "maxLength": 8192},
        "subject": {"type": "string", "minLength": 1, "maxLength": 512},
        "scope": {"type": "string", "minLength": 1, "maxLength": 256},
        "belief_type": {"type": "string", "enum": [item.value for item in BeliefType]},
        "portability": {"type": "string", "enum": [item.value for item in Portability]},
        "sensitivity": {"type": "string", "enum": [item.value for item in Sensitivity]},
    },
    "required": ["statement", "subject", "scope"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"belief_id": {"type": "string"}, "status": {"type": "string"}},
    "required": ["belief_id", "status"],
    "additionalProperties": False,
}


class MemoryRememberTool:
    spec = ToolSpec(
        name="memory.remember",
        version="1.0.0",
        description="Persist one user-sourced belief with provenance and governance metadata.",
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.MEDIUM,
        idempotency=IdempotencyClass.IDEMPOTENT,
        required_scopes=set(),
        timeout_seconds=10,
        maximum_output_bytes=4096,
        allow_parallel=False,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )

    def __init__(self, service: GovernedMemoryService) -> None:
        self._service = service

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        belief = await self._service.remember(
            session_id=context.session_id,
            run_id=context.run_id,
            statement=str(arguments["statement"]),
            subject=str(arguments["subject"]),
            scope=str(arguments["scope"]),
            belief_type=BeliefType(str(arguments.get("belief_type", BeliefType.FACT.value))),
            portability=(
                None
                if arguments.get("portability") is None
                else Portability(str(arguments["portability"]))
            ),
            sensitivity=Sensitivity(str(arguments.get("sensitivity", Sensitivity.INTERNAL.value))),
            origin_trust=context.origin_trust,
        )
        structured = {"belief_id": str(belief.id), "status": belief.status.value}
        return ToolResult(
            ok=True,
            content=[TextPart(text=f"Remembered as [m:{str(belief.id)[:8]}].")],
            structured=structured,
        )
