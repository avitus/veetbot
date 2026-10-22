"""Cited, owner-directed source learning through governed memory formation."""

import json
from typing import Any

from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.source_learning import SourceLearningArgs
from agent_core.domain.tools import ToolExecutionContext, ToolResult, ToolSpec
from agent_core.memory.source_learning import SourceLearningService


class MemoryLearnTool:
    spec = ToolSpec(
        name="memory.learn",
        version="1.1.0",
        description=(
            "Save owner-requested website observations as {call_id, quote} with an exact "
            "8-512 character excerpt; omit claim and subject to save that excerpt itself. "
            "Batch separate facts; cite a successful web.fetch, browser.navigate "
            "or browser.observe call_id and an exact quote for each claim. Claims must be "
            "a short contiguous phrase of that quote; do not paraphrase or infer identities. "
            "Do not claim to have inspected images from text-only reads. "
            "Search existing memories first; use reinforce_id only when the full tentative "
            "belief is supported. Saves remain attributed hypotheses. Reading again is not "
            "independent evidence. Report each returned disposition, never assume a save."
        ),
        input_schema=SourceLearningArgs.model_json_schema(),
        output_schema={
            "type": "object",
            "properties": {
                "results": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["results"],
            "additionalProperties": False,
        },
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.MEDIUM,
        idempotency=IdempotencyClass.IDEMPOTENT,
        required_scopes=set(),
        timeout_seconds=15,
        maximum_output_bytes=16384,
        allow_parallel=False,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )

    def __init__(self, service: SourceLearningService) -> None:
        self._service = service

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        results = await self._service.learn(SourceLearningArgs.model_validate(arguments), context)
        return ToolResult(
            ok=True,
            structured={"results": results},
            content=[
                TextPart(text=json.dumps({"results": results})),
            ],
        )


class LegacyMemoryLearnTool(MemoryLearnTool):
    """Keep the compatible handler available to sessions pinned before version 1.1."""

    spec = MemoryLearnTool.spec.model_copy(update={"version": "1.0.0"}, deep=True)
