"""JSON Schema validation and canonical argument normalization."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

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


def _canonical(value: object) -> object:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, float):
        if not math.isfinite(value) or (value == 0.0 and math.copysign(1.0, value) < 0):
            raise ToolValidationError("non-finite and negative-zero numbers are forbidden")
        return value
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {
            unicodedata.normalize("NFC", str(key)): _canonical(child)
            for key, child in sorted(value.items(), key=lambda item: str(item[0]).encode())
        }
    return value


def validate_and_normalize(
    arguments: dict[str, Any], schema: dict[str, Any]
) -> tuple[dict[str, Any], str, str]:
    """Apply defaults, validate, NFC-normalize, serialize, and hash arguments."""

    candidate = deepcopy(arguments)
    _apply_defaults(candidate, schema)
    try:
        Draft202012Validator(schema).validate(candidate)
    except ValidationError as exc:
        raise ToolValidationError("tool arguments do not match the declared schema") from exc
    normalized = _canonical(candidate)
    if not isinstance(normalized, dict):
        raise ToolValidationError("tool arguments must normalize to an object")
    rendered = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    return normalized, rendered, digest


def validate_output(structured: dict[str, Any] | None, schema: dict[str, Any] | None) -> None:
    if schema is None:
        return
    try:
        Draft202012Validator(schema).validate(structured)
    except ValidationError as exc:
        raise ToolValidationError("tool result does not match the declared schema") from exc
