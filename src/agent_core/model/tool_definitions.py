"""Provider wire definitions for model-visible tool contracts."""

from __future__ import annotations

from typing import Any

from agent_core.domain.tools import ToolSpec


def tool_definition(spec: object, *, anthropic: bool = False) -> dict[str, Any]:
    """Render only the tool fields a model provider actually receives."""

    tool = ToolSpec.model_validate(spec)
    if anthropic:
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.input_schema,
        "strict": True,
    }
