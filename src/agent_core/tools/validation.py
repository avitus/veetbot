"""JSON Schema validation and canonical argument normalization."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from agent_core.domain.canonical import canonical_json, canonical_value
from agent_core.domain.errors import ToolValidationError

MAX_SCHEMA_BYTES = 4 * 1024 * 1024
SUPPORTED_SCHEMA_DIALECTS = frozenset(
    {
        "https://json-schema.org/draft/2020-12/schema",
        "https://json-schema.org/draft/2020-12/schema#",
    }
)


def _walk(value: object) -> list[object]:
    result = [value]
    if isinstance(value, dict):
        for key, child in value.items():
            result.extend(_walk(key))
            result.extend(_walk(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_walk(child))
    return result


def validate_schema(schema: dict[str, Any]) -> None:
    """Validate Draft 2020-12 locally and reject network-resolved references."""

    dialect = schema.get("$schema")
    if dialect is not None and dialect not in SUPPORTED_SCHEMA_DIALECTS:
        raise ToolValidationError("tool schema must use JSON Schema Draft 2020-12")
    encoded = json.dumps(schema, ensure_ascii=False, sort_keys=True).encode()
    if len(encoded) > MAX_SCHEMA_BYTES:
        raise ToolValidationError("tool schema exceeds 4 MiB")
    for item in _walk(schema):
        if isinstance(item, dict):
            ref = item.get("$ref")
            if isinstance(ref, str) and "://" in ref:
                raise ToolValidationError("remote JSON Schema references are forbidden")
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise ToolValidationError("invalid Draft 2020-12 tool schema") from exc


def _apply_defaults(instance: object, schema: dict[str, Any]) -> object:
    if not isinstance(instance, dict) or schema.get("type") != "object":
        return instance
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return instance
    for name, subschema in properties.items():
        if not isinstance(name, str) or not isinstance(subschema, dict):
            continue
        if name not in instance and "default" in subschema:
            instance[name] = deepcopy(subschema["default"])
        if name in instance:
            instance[name] = _apply_defaults(instance[name], subschema)
    return instance


def validate_and_normalize(
    arguments: dict[str, Any], schema: dict[str, Any]
) -> tuple[dict[str, Any], str, str]:
    """Apply defaults, validate, NFC-normalize, serialize, and hash arguments."""

    candidate = deepcopy(arguments)
    _apply_defaults(candidate, schema)
    normalized = canonical_value(candidate)
    if not isinstance(normalized, dict):
        raise ToolValidationError("tool arguments must normalize to an object")
    try:
        Draft202012Validator(schema).validate(normalized)
    except ValidationError as exc:
        raise ToolValidationError("tool arguments do not match the declared schema") from exc
    rendered = canonical_json(normalized)
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    return normalized, rendered, digest


def validate_output(structured: dict[str, Any] | None, schema: dict[str, Any] | None) -> None:
    if schema is None:
        return
    try:
        Draft202012Validator(schema).validate(structured)
    except ValidationError as exc:
        raise ToolValidationError("tool result does not match the declared schema") from exc
