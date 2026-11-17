"""Read fixed platform instructions without executing model-authored commands."""

from importlib.resources import files
from typing import Any

from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import ToolExecutionContext, ToolResult, ToolSpec
from agent_core.tools.validation import validate_and_normalize


class ArtifactReadGuideTool:
    spec = ToolSpec(
        name="artifact.read_guide",
        version="1.0.0",
        description=(
            "Read bundled offline artifact instructions without approval. "
            "Sections: build, charts, diagrams."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "enum": ["build", "charts", "diagrams"],
                    "default": "build",
                }
            },
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        required_scopes=set(),
        timeout_seconds=10,
        maximum_output_bytes=16384,
        allow_parallel=False,
        output_trust=TrustLevel.PLATFORM,
    )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        del context
        arguments, _, _ = validate_and_normalize(arguments, self.spec.input_schema)
        guide = (
            files("agent_core.execution")
            .joinpath("visualization-guide.md")
            .read_text(encoding="utf-8")
        )
        build, remainder = guide.split("## Charts\n", 1)
        charts, diagrams = remainder.split("## Diagrams\n", 1)
        text = {
            "build": build,
            "charts": "## Charts\n" + charts,
            "diagrams": "## Diagrams\n" + diagrams,
        }[arguments["section"]]
        return ToolResult(
            ok=True,
            content=[TextPart(text=text)],
            structured={"section": arguments["section"], "text": text},
        )
