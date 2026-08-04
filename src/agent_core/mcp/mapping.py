"""Pure MCP discovery mapping into validated platform tool specifications."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass

from agent_core.context.estimator import canonical_json_bytes
from agent_core.domain.errors import ToolValidationError
from agent_core.domain.mcp import MCPRemoteTool, MCPServerConfig
from agent_core.domain.policies import TrustLevel
from agent_core.domain.tools import ToolSource, ToolSpec
from agent_core.tools.registry import validate_registration
from agent_core.tools.validation import validate_schema

_UNSAFE_NAME = re.compile(r"[^a-z0-9_]")
_UNDERSCORES = re.compile(r"_+")


@dataclass(frozen=True, slots=True)
class MappedRemoteTool:
    remote_name: str
    spec: ToolSpec


@dataclass(frozen=True, slots=True)
class MCPMappingReport:
    catalog_hash: str
    accepted: tuple[MappedRemoteTool, ...]
    rejected: tuple[str, ...]
    conflicts: tuple[tuple[str, ...], ...]


def normalize_remote_name(remote_name: str) -> str:
    normalized = unicodedata.normalize("NFC", remote_name).lower()
    normalized = _UNDERSCORES.sub("_", _UNSAFE_NAME.sub("_", normalized)).strip("_")
    if not normalized or normalized[0].isdigit():
        normalized = f"t_{normalized}"
    if len(normalized) > 48:
        suffix = hashlib.sha256(remote_name.encode("utf-8")).hexdigest()[:7]
        normalized = f"{normalized[:40]}_{suffix}"
    return normalized


def _inspect_schema(value: object, maximum_depth: int) -> tuple[int, bool]:
    """Inspect untrusted schema structure without using Python recursion."""

    deepest = 1
    contains_ref = False
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        deepest = max(deepest, depth)
        if deepest > maximum_depth:
            return deepest, contains_ref
        if isinstance(current, dict):
            contains_ref = contains_ref or "$ref" in current
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    return deepest, contains_ref


def _truncate_utf8(text: str, maximum_bytes: int) -> str:
    return text.encode("utf-8")[:maximum_bytes].decode("utf-8", errors="ignore")


def map_discovered_tools(
    config: MCPServerConfig,
    remote_tools: tuple[MCPRemoteTool, ...],
    *,
    description_maximum_bytes: int = 1_024,
    schema_maximum_depth: int = 16,
    schema_maximum_bytes: int = 32_768,
) -> MCPMappingReport:
    schema_inspections = {
        id(tool): _inspect_schema(tool.input_schema, schema_maximum_depth) for tool in remote_tools
    }
    declarations = sorted(
        (
            tool.model_dump(mode="json")
            for tool in remote_tools
            if schema_inspections[id(tool)][0] <= schema_maximum_depth
        ),
        key=lambda item: (str(item["name"]), canonical_json_bytes(item)),
    )
    catalog_hash = hashlib.sha256(canonical_json_bytes(declarations)).hexdigest()
    by_registry_name: dict[str, list[MCPRemoteTool]] = {}
    for remote in remote_tools:
        registry_name = f"mcp.{config.server_id}.{normalize_remote_name(remote.name)}"
        by_registry_name.setdefault(registry_name, []).append(remote)
    conflicts = tuple(
        tuple(sorted(item.name for item in colliding))
        for _name, colliding in sorted(by_registry_name.items())
        if len(colliding) > 1
    )
    colliding_names = {name for group in conflicts for name in group}
    accepted: list[MappedRemoteTool] = []
    rejected: list[str] = []
    for remote in sorted(remote_tools, key=lambda item: item.name):
        if remote.name in colliding_names:
            continue
        try:
            depth, contains_ref = schema_inspections[id(remote)]
            if depth > schema_maximum_depth:
                raise ToolValidationError("MCP input schema exceeds depth 16")
            encoded = json.dumps(remote.input_schema, ensure_ascii=False, sort_keys=True).encode()
            if len(encoded) > schema_maximum_bytes:
                raise ToolValidationError("MCP input schema exceeds 32 KiB")
            if contains_ref:
                raise ToolValidationError("MCP input schema references are forbidden")
            validate_schema(remote.input_schema)
            spec = validate_registration(
                ToolSpec(
                    name=f"mcp.{config.server_id}.{normalize_remote_name(remote.name)}",
                    version=catalog_hash,
                    description=_truncate_utf8(remote.description, description_maximum_bytes),
                    input_schema=remote.input_schema,
                    output_schema=None,
                    side_effect=config.side_effect,
                    risk=config.risk,
                    idempotency=config.idempotency,
                    required_scopes=set(config.required_scopes),
                    timeout_seconds=config.timeout_seconds,
                    maximum_output_bytes=config.maximum_output_bytes,
                    allow_parallel=False,
                    target_kind="mcp",
                    output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
                    source=ToolSource.MCP,
                    server_id=config.server_id,
                )
            )
        except (ToolValidationError, ValueError):
            rejected.append(remote.name)
            continue
        accepted.append(MappedRemoteTool(remote_name=remote.name, spec=spec))
    return MCPMappingReport(
        catalog_hash=catalog_hash,
        accepted=tuple(accepted),
        rejected=tuple(sorted(rejected)),
        conflicts=conflicts,
    )
