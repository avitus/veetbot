"""Read-only view of the published OpenAPI document; every target stays under the API root."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from svp_mcp.constants import API_BASE, API_ROOT, MAXIMUM_DESCRIPTION_BYTES, ORIGIN
from svp_mcp.errors import SvpError

_OPERATION_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_PLACEHOLDER = re.compile(r"\{([^{}/]+)\}")
_UNSAFE_URL_CHARACTER = re.compile(r"[\s\\\x00-\x1f\x7f]")
_API_PATH = urlsplit(API_ROOT).path
_HOST = urlsplit(ORIGIN).hostname
_PARAMETER_REFERENCE = "#/components/parameters/"
_SCHEMA_REFERENCE = "#/components/schemas/"
_SUMMARY_CHARACTERS = 300
_DESCRIPTION_CHARACTERS = 2000
_REFERENCE_DEPTH = 8


@dataclass(frozen=True)
class Parameter:
    name: str
    location: str
    required: bool
    schema: Any
    description: str


@dataclass(frozen=True)
class Operation:
    operation_id: str
    path: str
    summary: str
    description: str
    parameters: tuple[Parameter, ...]
    response_schema: Any


def is_confined(url: str) -> bool:
    """Accept only HTTPS on the fixed host and default port, under the API root."""

    if not isinstance(url, str) or _UNSAFE_URL_CHARACTER.search(url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or parts.hostname != _HOST or port is not None:
        return False
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        return False
    if "?" in url or "#" in url or not parts.path.startswith(_API_PATH):
        return False
    return not any(unquote(segment) in {".", ".."} for segment in parts.path.split("/"))


def _url(path: str) -> str:
    return API_BASE + path


def _text(value: object, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _parameters(document: Mapping[str, Any], item: Mapping[str, Any], path: str) -> list[Parameter]:
    components = document.get("components")
    shared = components.get("parameters") if isinstance(components, dict) else None
    merged: dict[tuple[str, str], Parameter] = {}
    placeholders = _PLACEHOLDER.findall(path)
    for source in (item.get("parameters"), item["get"].get("parameters")):
        for declared in source if isinstance(source, list) else ():
            reference = declared.get("$ref") if isinstance(declared, dict) else None
            if isinstance(reference, str) and reference.startswith(_PARAMETER_REFERENCE):
                target = reference.removeprefix(_PARAMETER_REFERENCE)
                declared = shared.get(target) if isinstance(shared, dict) else None
            if not isinstance(declared, dict):
                continue
            name, location = declared.get("name"), declared.get("in")
            if not isinstance(name, str) or not name or location not in {"path", "query"}:
                continue
            if location == "path" and name not in placeholders:
                continue
            schema = declared.get("schema")
            merged[(name, location)] = Parameter(
                name=name,
                location=location,
                required=location == "path" or declared.get("required") is True,
                schema=schema if isinstance(schema, dict) else {},
                description=_text(declared.get("description"), _SUMMARY_CHARACTERS),
            )
    for name in placeholders:
        merged.setdefault((name, "path"), Parameter(name, "path", True, {}, ""))
    return list(merged.values())


def _response_schema(operation: Mapping[str, Any]) -> Any:
    value: Any = operation
    for key in ("responses", "200", "content", "application/json", "schema"):
        value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, dict) else None


def load_operations(document: object) -> dict[str, Operation]:
    """Index the document's `GET` operations whose paths stay under the API root."""

    paths = document.get("paths") if isinstance(document, dict) else None
    if not isinstance(document, dict) or not isinstance(paths, dict):
        raise SvpError("svp.specification_invalid")
    operations: dict[str, Operation] = {}
    for path in sorted(key for key in paths if isinstance(key, str)):
        item = paths[path]
        operation = item.get("get") if isinstance(item, dict) else None
        if not isinstance(operation, dict) or not path.startswith("/") or path.startswith("//"):
            continue
        if not is_confined(_url(_PLACEHOLDER.sub("x", path))):
            continue
        identifier = operation.get("operationId")
        if (
            not isinstance(identifier, str)
            or _OPERATION_ID.fullmatch(identifier) is None
            or identifier in operations
        ):
            base = "get_" + re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_")
            identifier, suffix = base, 2
            while identifier in operations:
                identifier, suffix = f"{base}_{suffix}", suffix + 1
        operations[identifier] = Operation(
            operation_id=identifier,
            path=path,
            summary=_text(operation.get("summary"), _SUMMARY_CHARACTERS),
            description=_text(operation.get("description"), _DESCRIPTION_CHARACTERS),
            parameters=tuple(_parameters(document, item, path)),
            response_schema=_response_schema(operation),
        )
    return operations


def _path_value(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise SvpError("svp.arguments_invalid")
    text = str(value)
    if not text or text in {".", ".."} or _UNSAFE_URL_CHARACTER.search(text.replace(" ", "")):
        raise SvpError("svp.arguments_invalid")
    return text


def _query_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and not math.isfinite(value):
        raise SvpError("svp.arguments_invalid")
    if not isinstance(value, str | int | float):
        raise SvpError("svp.arguments_invalid")
    return str(value)


def request_target(
    operation: Operation,
    path_parameters: Mapping[str, object],
    query: Mapping[str, object],
) -> tuple[str, list[tuple[str, str]]]:
    """Build one confined URL and its declared query pairs, or refuse the arguments."""

    if not isinstance(path_parameters, Mapping) or not isinstance(query, Mapping):
        raise SvpError("svp.arguments_invalid")
    declared_path = {item.name for item in operation.parameters if item.location == "path"}
    declared_query = {item.name: item for item in operation.parameters if item.location == "query"}
    if set(path_parameters) != declared_path or not set(query) <= set(declared_query):
        raise SvpError("svp.arguments_invalid")
    path = _PLACEHOLDER.sub(
        lambda match: quote(_path_value(path_parameters[match.group(1)]), safe=""),
        operation.path,
    )
    pairs: list[tuple[str, str]] = []
    for name, value in query.items():
        if value is None:
            continue
        values = value if isinstance(value, list | tuple) else (value,)
        pairs.extend((name, _query_value(item)) for item in values)
    supplied = {name for name, _value in pairs}
    if any(item.required and name not in supplied for name, item in declared_query.items()):
        raise SvpError("svp.arguments_invalid")
    url = _url(path)
    if not is_confined(url):
        raise SvpError("svp.arguments_invalid")
    return url, pairs


def _resolved(document: Mapping[str, Any], value: Any, seen: frozenset[str], depth: int) -> Any:
    if isinstance(value, list):
        return [_resolved(document, item, seen, depth) for item in value]
    if not isinstance(value, dict):
        return value
    reference = value.get("$ref")
    if isinstance(reference, str) and reference.startswith(_SCHEMA_REFERENCE):
        components = document.get("components")
        schemas = components.get("schemas") if isinstance(components, dict) else None
        target = (
            schemas.get(reference.removeprefix(_SCHEMA_REFERENCE))
            if isinstance(schemas, dict)
            else None
        )
        if reference in seen or depth >= _REFERENCE_DEPTH or not isinstance(target, dict):
            return {"$ref": reference}
        return _resolved(document, target, seen | {reference}, depth + 1)
    return {key: _resolved(document, item, seen, depth) for key, item in value.items()}


def describe(document: Mapping[str, Any], operation: Operation) -> dict[str, Any]:
    """Describe one operation with its local schema references resolved."""

    described: dict[str, Any] = {
        "id": operation.operation_id,
        "method": "GET",
        "path": operation.path,
        "summary": operation.summary,
        "description": operation.description,
        "parameters": [
            {
                "name": item.name,
                "in": item.location,
                "required": item.required,
                "schema": _resolved(document, item.schema, frozenset(), 0),
                "description": item.description,
            }
            for item in operation.parameters
        ],
        "response_schema": _resolved(document, operation.response_schema, frozenset(), 0),
        "response_schema_omitted": False,
    }
    if len(json.dumps(described, separators=(",", ":")).encode()) > MAXIMUM_DESCRIPTION_BYTES:
        described["response_schema"] = None
        described["response_schema_omitted"] = True
    if len(json.dumps(described, separators=(",", ":")).encode()) > MAXIMUM_DESCRIPTION_BYTES:
        raise SvpError("svp.response_too_large")
    return described
