"""Control tool for loading and unloading pinned skill content."""

from __future__ import annotations

from typing import Any

from agent_core.domain.errors import AgentCoreError, NotFoundError
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import (
    IdempotencyClass,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.skills import SKILL_NAME_PATTERN, LoadedSkillBody
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolKind,
    ToolResult,
    ToolSpec,
)
from agent_core.ports.skills import SkillCatalog

SKILL_LOAD_TOOL_NAME = "skill.load"


class SkillLoadTool:
    spec = ToolSpec(
        name=SKILL_LOAD_TOOL_NAME,
        version="1.1.0",
        description=(
            "Load or unload an exact skill name listed in Available skill metadata; "
            "never guess names or use this tool to discover capabilities."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact name from Available skill metadata.",
                    "minLength": 1,
                    "maxLength": 64,
                },
                "path": {"type": "string", "minLength": 1, "maxLength": 512},
                "unload": {"type": "boolean"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        output_schema=None,
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.IDEMPOTENT,
        required_scopes=set(),
        timeout_seconds=10,
        maximum_output_bytes=1_048_576,
        allow_parallel=False,
        kind=ToolKind.CONTROL,
        target_kind="in_process",
        output_trust=TrustLevel.PLATFORM,
    )

    def __init__(self, catalogs: SkillCatalog) -> None:
        self._catalogs = catalogs

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        name = str(arguments["name"])
        if SKILL_NAME_PATTERN.fullmatch(name) is None:
            return ToolResult(
                ok=False,
                content=[TextPart(text="The requested skill name is invalid.")],
                failure=ToolFailure(
                    kind=ToolFailureKind.INVALID_ARGUMENTS,
                    reason_code="tool.arguments_invalid",
                    detail="skill name does not match the required grammar",
                    retryable=False,
                ),
            )
        try:
            catalog = self._catalogs.current(context.session_id)
        except NotFoundError:
            catalog = None
        if catalog is not None:
            available_names = tuple(entry.manifest.name for entry in catalog.entries)
            if name not in available_names:
                visible = ", ".join(available_names) or "none"
                return ToolResult(
                    ok=False,
                    content=[],
                    failure=ToolFailure(
                        kind=ToolFailureKind.NOT_FOUND,
                        reason_code="tool.skill.not_in_catalog",
                        detail=f"skill {name!r} is not in the pinned catalog",
                        retryable=False,
                        external_text=f"Available skill names: {visible}",
                    ),
                )
        if bool(arguments.get("unload", False)):
            return ToolResult(
                ok=True,
                content=[TextPart(text=f"Unloaded skill {name}.")],
                structured={"skill_update": {"operation": "unload", "name": name}},
            )
        loaded = tuple(LoadedSkillBody.model_validate(item) for item in context.loaded_skills)
        try:
            body, missing = await self._catalogs.load(
                context.session_id,
                context.principal,
                name,
                None if arguments.get("path") is None else str(arguments["path"]),
                loaded,
                context.available_tools,
            )
        except (AgentCoreError, ValueError) as exc:
            return ToolResult(
                ok=False,
                content=[TextPart(text=str(exc))],
                failure=ToolFailure(
                    kind=ToolFailureKind.INVALID_ARGUMENTS,
                    reason_code="tool.arguments_invalid",
                    detail=str(exc),
                    retryable=False,
                ),
            )
        header = (
            f"Loaded skill {body.name}@{body.revision}. "
            f"Missing required tools: {', '.join(missing) if missing else 'none'}."
        )
        return ToolResult(
            ok=True,
            content=[TextPart(text=header), TextPart(text=body.content)],
            structured={
                "skill_update": {
                    "operation": "load",
                    "body": body.model_dump(mode="json"),
                    "missing_tools": list(missing),
                    "notes": (["skill.tool.missing"] if missing else []),
                }
            },
            output_trust=body.trust,
        )


class LegacySkillLoadTool(SkillLoadTool):
    """Compatibility registration for sessions pinned before the 1.1.0 revision."""

    spec = ToolSpec(
        name=SKILL_LOAD_TOOL_NAME,
        version="1.0.0",
        description="Load or unload content from the session-pinned skill catalog.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 64},
                "path": {"type": "string", "minLength": 1, "maxLength": 512},
                "unload": {"type": "boolean"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        output_schema=None,
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.IDEMPOTENT,
        required_scopes=set(),
        timeout_seconds=10,
        maximum_output_bytes=1_048_576,
        allow_parallel=False,
        kind=ToolKind.CONTROL,
        target_kind="in_process",
        output_trust=TrustLevel.PLATFORM,
    )
