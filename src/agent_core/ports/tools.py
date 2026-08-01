"""Tool implementation and registry ports."""

from __future__ import annotations

from typing import Any, Protocol

from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.tools import ToolExecutionContext, ToolResult, ToolSpec


class Tool(Protocol):
    spec: ToolSpec

    async def execute(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> ToolResult: ...


class ToolRegistry(Protocol):
    def get(self, name: str, version: str | None = None) -> Tool: ...

    def specs_for_session(
        self,
        agent: AgentSpec,
        principal: Principal,
        profile: object,
        environment: object,
    ) -> list[ToolSpec]: ...
