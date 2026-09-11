"""Provider wire definitions for model-visible tool contracts."""

from __future__ import annotations

from typing import Any

from agent_core.domain.tools import ToolSpec


def _compact_schema(schema: dict[str, Any], label: str) -> dict[str, Any]:
    """Omit generated label repetitions without removing meaningful schema annotations."""

    result = dict(schema)
    title = result.get("title")

    def normalize(value: str) -> str:
        return value.replace("_", "").replace(" ", "").casefold()

    if isinstance(title, str) and normalize(title) == normalize(label):
        result.pop("title")
    for key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
        children = result.get(key)
        if isinstance(children, dict):
            result[key] = {
                name: _compact_schema(value, name) if isinstance(value, dict) else value
                for name, value in children.items()
            }
    for key in (
        "items",
        "additionalProperties",
        "contains",
        "propertyNames",
        "not",
        "if",
        "then",
        "else",
    ):
        child = result.get(key)
        if isinstance(child, dict):
            result[key] = _compact_schema(child, label)
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        children = result.get(key)
        if isinstance(children, list):
            result[key] = [
                _compact_schema(child, label) if isinstance(child, dict) else child
                for child in children
            ]
    return result


def tool_definition(spec: object, *, anthropic: bool = False) -> dict[str, Any]:
    """Render only the tool fields a model provider actually receives."""

    tool = ToolSpec.model_validate(spec)
    schema = _compact_schema(tool.input_schema, tool.name.rsplit(".", 1)[-1] + "Arguments")
    if anthropic:
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": schema,
        }
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": schema,
        "strict": True,
    }
