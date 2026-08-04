"""Validated, namespaced registry for model-callable tools."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.errors import ConflictError, NotFoundError, ToolValidationError
from agent_core.domain.policies import IdempotencyClass, SideEffectClass, TrustLevel
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolKind,
    ToolResult,
    ToolSource,
    ToolSpec,
)
from agent_core.policy.scopes import validate_required_scopes
from agent_core.ports.tools import Tool
from agent_core.tools.validation import validate_schema

TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
BUILTIN_DOMAINS = frozenset(
    {
        "system",
        "math",
        "workspace",
        "sandbox",
        "artifact",
        "demo",
        "delegate",
        "conversation",
        "context",
        "skill",
        "memory",
        "knowledge",
    }
)
RESERVED_DOMAINS = frozenset({"mcp", "device"})
GLOBAL_MAXIMUM_OUTPUT_BYTES = 4 * 1024 * 1024


@dataclass(slots=True)
class RegisteredTool:
    spec: ToolSpec
    implementation: Tool

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        return await self.implementation.execute(arguments, context)


def validate_registration(spec: ToolSpec) -> ToolSpec:
    """Apply the seven ordered startup refusals to a declared tool."""

    if len(spec.name) > 96 or TOOL_NAME.fullmatch(spec.name) is None:
        raise ToolValidationError("invalid tool name")
    domain = spec.name.partition(".")[0]
    if spec.source is ToolSource.BUILTIN and domain in RESERVED_DOMAINS:
        raise ToolValidationError("builtin tools may not use reserved domains")
    if spec.source is ToolSource.BUILTIN and domain not in BUILTIN_DOMAINS:
        raise ToolValidationError("unknown builtin tool domain")
    if spec.source is not ToolSource.BUILTIN and domain in BUILTIN_DOMAINS:
        raise ToolValidationError("external tools may not use builtin-owned domains")
    if spec.source is ToolSource.MCP and domain != "mcp":
        raise ToolValidationError("MCP tools must use the mcp namespace")
    if spec.source is ToolSource.MCP:
        if spec.server_id is None or re.fullmatch(r"[a-z][a-z0-9_]*", spec.server_id) is None:
            raise ToolValidationError("MCP tools require a valid server id")
        if not spec.name.startswith(f"mcp.{spec.server_id}."):
            raise ToolValidationError("MCP tool name does not match its server id")
    if spec.source is ToolSource.DEVICE and domain != "device":
        raise ToolValidationError("device tools must use the device namespace")
    validate_required_scopes(
        spec.required_scopes,
        mcp_server_id=spec.server_id if spec.source is ToolSource.MCP else None,
    )
    validate_schema(spec.input_schema)
    if spec.output_schema is not None:
        validate_schema(spec.output_schema)
    if spec.timeout_seconds <= 0 or spec.maximum_output_bytes <= 0:
        raise ToolValidationError("tool limits must be positive")
    if spec.maximum_output_bytes > GLOBAL_MAXIMUM_OUTPUT_BYTES:
        raise ToolValidationError("tool output limit exceeds the global ceiling")
    if spec.kind is ToolKind.CONTROL and (
        spec.side_effect is not SideEffectClass.NONE
        or spec.idempotency not in {IdempotencyClass.READ_ONLY, IdempotencyClass.IDEMPOTENT}
        or spec.target_kind != "in_process"
    ):
        raise ToolValidationError("control tool classification is invalid")
    if spec.source in {ToolSource.MCP, ToolSource.DEVICE, ToolSource.SANDBOX} or (
        spec.target_kind == "sandbox"
    ):
        return spec.model_copy(update={"output_trust": TrustLevel.EXTERNAL_UNTRUSTED})
    return spec


class StaticToolRegistry:
    """A build-time catalog whose advertisement is filtered but never rewritten."""

    def __init__(self) -> None:
        self._tools: dict[tuple[str, str], RegisteredTool] = {}
        self._latest: dict[str, str] = {}

    def register(self, tool: Tool) -> None:
        spec = validate_registration(tool.spec)
        key = (spec.name, spec.version)
        if key in self._tools:
            raise ConflictError("duplicate tool name and version")
        self._tools[key] = RegisteredTool(spec=spec, implementation=tool)
        self._latest[spec.name] = spec.version

    def get(self, name: str, version: str | None = None) -> Tool:
        """Return the selected tool or raise NotFoundError when it is unavailable."""

        selected_version = self._latest.get(name) if version is None else version
        if selected_version is None:
            raise NotFoundError("tool not found")
        try:
            return self._tools[(name, selected_version)]
        except KeyError as exc:
            raise NotFoundError("tool not found") from exc

    def specs_for_session(
        self,
        agent: AgentSpec,
        principal: Principal,
        profile: object,
        environment: object,
    ) -> list[ToolSpec]:
        del profile, environment
        result: list[ToolSpec] = []
        for name in agent.enabled_tools:
            try:
                spec = self.get(name).spec
            except NotFoundError:
                continue
            if spec.deprecated or not spec.required_scopes.issubset(principal.scopes):
                continue
            result.append(spec.model_copy(deep=True))
        return sorted(result, key=lambda spec: spec.name)
