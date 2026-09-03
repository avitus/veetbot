"""Tool implementation and registry ports."""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.devices import DeviceInvocation
from agent_core.domain.tools import ToolExecutionContext, ToolResult, ToolSource, ToolSpec


class Tool(Protocol):
    spec: ToolSpec

    async def execute(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> ToolResult: ...


class ToolRegistry(Protocol):
    def register_dynamic(
        self,
        tool: Tool,
        *,
        tenant_id: str,
        principal_id: str | None = None,
    ) -> None:
        """Register an MCP tool per tenant or a device tool per principal."""
        ...

    def unregister_dynamic(
        self,
        name: str,
        version: str,
        *,
        tenant_id: str,
        principal_id: str | None = None,
    ) -> None:
        """Remove one dynamic registration when its last session closes."""
        ...

    def get(
        self,
        name: str,
        version: str | None = None,
        *,
        tenant_id: str | None = None,
        principal_id: str | None = None,
        source: ToolSource | None = None,
        server_id: str | None = None,
        device_id: str | None = None,
    ) -> Tool:
        """Return the selected tool or raise NotFoundError when unavailable."""
        ...

    def specs_for_session(
        self,
        agent: AgentSpec,
        principal: Principal,
        profile: object,
        environment: object,
    ) -> list[ToolSpec]: ...


class DeviceChannel(Protocol):
    """Invoke a device-scoped tool on a specific device and return its result."""

    async def invoke(
        self,
        *,
        device_id: UUID,
        run_id: UUID,
        invocation_id: UUID,
        tool_name: str,
        arguments: dict[str, Any],
        principal: Principal,
    ) -> DeviceInvocation:
        """Return the terminal invocation, or its expired row when the device stays silent.

        Raises ``DeviceChannelUnavailable`` when the named device is not
        present for this principal or does not grant the named capability.
        """
        ...
