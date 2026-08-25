"""Validated, namespaced registry for model-callable tools."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, cast

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.errors import ConflictError, NotFoundError, ToolValidationError
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
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
        "web",
        "browser",
        "schedule",
    }
)
RESERVED_DOMAINS = frozenset({"mcp", "device"})
GLOBAL_MAXIMUM_OUTPUT_BYTES = 4 * 1024 * 1024
CONTROL_TOOL_NAMES = frozenset(
    {
        "conversation.ask_user",
        "delegate.run",
        "context.update_working_state",
        "skill.load",
    }
)


@dataclass(slots=True)
class RegisteredTool:
    spec: ToolSpec
    implementation: Tool

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        return await self.implementation.execute(arguments, context)

    async def approval_view(
        self, arguments: dict[str, Any], *, tenant_id: str
    ) -> tuple[str, dict[str, Any]]:
        presenter = getattr(self.implementation, "approval_view", None)
        if presenter is None:
            return f"Run {self.spec.name} with validated arguments.", arguments
        return cast(
            tuple[str, dict[str, Any]],
            await presenter(arguments, tenant_id=tenant_id),
        )


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
        spec.name not in CONTROL_TOOL_NAMES
        or spec.side_effect is not SideEffectClass.NONE
        or spec.idempotency not in {IdempotencyClass.READ_ONLY, IdempotencyClass.IDEMPOTENT}
        or spec.target_kind != "in_process"
    ):
        raise ToolValidationError("control tool classification is invalid")
    if spec.name in CONTROL_TOOL_NAMES and spec.kind is not ToolKind.CONTROL:
        raise ToolValidationError("declared control tool must use the control kind")
    if spec.target_kind == "web_provider" and (
        spec.source is not ToolSource.BUILTIN
        or domain != "web"
        or spec.side_effect is not SideEffectClass.NETWORK_READ
        or spec.idempotency is not IdempotencyClass.READ_ONLY
        or spec.output_trust is not TrustLevel.EXTERNAL_UNTRUSTED
    ):
        raise ToolValidationError("web provider target classification is invalid")
    if spec.target_kind == "browser_provider":
        browser_read = (
            spec.name in {"browser.navigate", "browser.observe"}
            and spec.side_effect is SideEffectClass.NETWORK_READ
            and spec.risk is RiskLevel.LOW
            and spec.idempotency is IdempotencyClass.READ_ONLY
            and not spec.allow_parallel
        )
        browser_write = (
            spec.name == "browser.act"
            and spec.side_effect is SideEffectClass.EXTERNAL_WRITE
            and spec.risk is RiskLevel.HIGH
            and spec.idempotency is IdempotencyClass.NON_IDEMPOTENT
            and not spec.allow_parallel
        )
        if (
            spec.source is not ToolSource.BUILTIN
            or domain != "browser"
            or not (browser_read or browser_write)
            or spec.output_trust is not TrustLevel.EXTERNAL_UNTRUSTED
        ):
            raise ToolValidationError("browser provider target classification is invalid")
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
        self._dynamic_tools: dict[tuple[str, str, str], RegisteredTool] = {}
        self._dynamic_latest: dict[tuple[str, str], str] = {}

    def register(self, tool: Tool) -> None:
        spec = validate_registration(tool.spec)
        key = (spec.name, spec.version)
        if key in self._tools or any(name == spec.name for _, name, _ in self._dynamic_tools):
            raise ConflictError("duplicate tool name and version")
        self._tools[key] = RegisteredTool(spec=spec, implementation=tool)
        self._latest[spec.name] = spec.version

    def register_dynamic(self, tool: Tool, *, tenant_id: str) -> None:
        spec = validate_registration(tool.spec)
        if spec.source is not ToolSource.MCP:
            raise ToolValidationError("only MCP discovery may dynamically register tools")
        if not tenant_id:
            raise ToolValidationError("dynamic tool registration requires a tenant")
        if spec.name in self._latest:
            raise ConflictError("dynamic tool name is reserved by a static registration")
        key = (tenant_id, spec.name, spec.version)
        if key in self._dynamic_tools:
            raise ConflictError("duplicate dynamic tool name and version")
        self._dynamic_tools[key] = RegisteredTool(spec=spec, implementation=tool)
        self._dynamic_latest[(tenant_id, spec.name)] = spec.version

    def unregister_dynamic(self, name: str, version: str, *, tenant_id: str) -> None:
        key = (tenant_id, name, version)
        self._dynamic_tools.pop(key, None)
        latest_key = (tenant_id, name)
        if self._dynamic_latest.get(latest_key) != version:
            return
        remaining = sorted(
            candidate_version
            for candidate_tenant, candidate_name, candidate_version in self._dynamic_tools
            if candidate_tenant == tenant_id and candidate_name == name
        )
        if remaining:
            self._dynamic_latest[latest_key] = remaining[-1]
        else:
            self._dynamic_latest.pop(latest_key, None)

    def get(
        self,
        name: str,
        version: str | None = None,
        *,
        tenant_id: str | None = None,
        source: ToolSource | None = None,
        server_id: str | None = None,
    ) -> Tool:
        """Return the selected tool or raise NotFoundError when it is unavailable."""

        if tenant_id is not None:
            dynamic_version = (
                self._dynamic_latest.get((tenant_id, name)) if version is None else version
            )
            if dynamic_version is not None:
                dynamic = self._dynamic_tools.get((tenant_id, name, dynamic_version))
                if dynamic is not None and self._identity_matches(
                    dynamic.spec, source=source, server_id=server_id
                ):
                    return dynamic
        selected_version = self._latest.get(name) if version is None else version
        if selected_version is None:
            raise NotFoundError("tool not found")
        try:
            tool = self._tools[(name, selected_version)]
        except KeyError as exc:
            raise NotFoundError("tool not found") from exc
        if not self._identity_matches(tool.spec, source=source, server_id=server_id):
            raise NotFoundError("tool not found")
        return tool

    @staticmethod
    def _identity_matches(
        spec: ToolSpec,
        *,
        source: ToolSource | None,
        server_id: str | None,
    ) -> bool:
        return (source is None or spec.source is source) and (
            server_id is None or spec.server_id == server_id
        )

    def specs_for_session(
        self,
        agent: AgentSpec,
        principal: Principal,
        profile: object,
        environment: object,
    ) -> list[ToolSpec]:
        del profile, environment
        result: list[ToolSpec] = []
        names = [
            *agent.enabled_tools,
            *sorted(
                name for tenant_id, name in self._dynamic_latest if tenant_id == principal.tenant_id
            ),
        ]
        for name in dict.fromkeys(names):
            try:
                spec = self.get(name, tenant_id=principal.tenant_id).spec
            except NotFoundError:
                continue
            if spec.deprecated or not spec.required_scopes.issubset(principal.scopes):
                continue
            result.append(spec.model_copy(deep=True))
        return sorted(result, key=lambda spec: spec.name)
