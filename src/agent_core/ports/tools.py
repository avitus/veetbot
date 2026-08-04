"""Tool implementation and registry ports."""

from __future__ import annotations

from typing import Any, Protocol

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.tools import ToolExecutionContext, ToolResult, ToolSource, ToolSpec


class Tool(Protocol):
    spec: ToolSpec

    async def execute(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> ToolResult: ...


class ToolRegistry(Protocol):
    def register_dynamic(self, tool: Tool, *, tenant_id: str) -> None:
        """Register an MCP-discovered implementation for one tenant."""
        ...

    def unregister_dynamic(self, name: str, version: str, *, tenant_id: str) -> None:
        """Remove one tenant-scoped MCP registration when its last session closes."""
        ...

    def get(
        self,
        name: str,
        version: str | None = None,
        *,
        tenant_id: str | None = None,
        source: ToolSource | None = None,
        server_id: str | None = None,
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
