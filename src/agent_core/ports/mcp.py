"""Adapter-neutral MCP client and configuration repository ports."""

from __future__ import annotations

from types import TracebackType
from typing import Any, Protocol, Self

from agent_core.domain.credentials import SecretValue
from agent_core.domain.mcp import (
    MCPCallResult,
    MCPDiscovery,
    MCPServerConfig,
    MCPToolCatalogRecord,
)


class MCPClient(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...

    async def discover(self) -> MCPDiscovery: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPCallResult: ...

    async def read_resource(self, uri: str | None) -> MCPCallResult: ...

    async def reauthenticate(
        self,
        credential: SecretValue | None,
        environment: dict[str, str],
    ) -> bool:
        """Reconnect and return whether credential bytes changed."""
        ...


class MCPClientFactory(Protocol):
    def __call__(
        self,
        config: MCPServerConfig,
        credential: SecretValue | None,
        environment: dict[str, str],
    ) -> MCPClient: ...


class MCPServerRepository(Protocol):
    async def put(self, config: MCPServerConfig) -> None: ...

    async def list_enabled(self, tenant_id: str) -> list[MCPServerConfig]: ...

    async def record_catalog(
        self,
        tenant_id: str,
        server_id: str,
        catalog_hash: str,
        records: tuple[MCPToolCatalogRecord, ...],
    ) -> None: ...
