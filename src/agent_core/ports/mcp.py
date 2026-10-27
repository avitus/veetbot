"""Adapter-neutral MCP client and configuration repository ports."""

from __future__ import annotations

from types import TracebackType
from typing import Any, Protocol, Self

from agent_core.domain.credentials import SecretValue
from agent_core.domain.mcp import (
    MCPAuthorizationMetadata,
    MCPCallResult,
    MCPClientRegistration,
    MCPDiscovery,
    MCPProbeResult,
    MCPServerAuthorization,
    MCPServerConfig,
    MCPTokenGrant,
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

    async def force_close(self) -> None:
        """Stop owned transports after graceful cleanup expires; repeated calls are harmless."""
        ...

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

    async def get(self, tenant_id: str, server_id: str) -> MCPServerConfig | None: ...

    async def delete(self, tenant_id: str, server_id: str) -> bool:
        """Remove a server with its catalog history and sign-in (ADR-0255)."""
        ...

    async def get_authorization(
        self, tenant_id: str, server_id: str, *, for_update: bool = False
    ) -> MCPServerAuthorization | None:
        """Read one server's sealed sign-in; ``for_update`` serializes token refresh."""
        ...

    async def find_authorization_by_state(
        self, tenant_id: str, state_hash: str
    ) -> MCPServerAuthorization | None:
        """Lock the sign-in a callback's state names, if any (ADR-0255)."""
        ...

    async def put_authorization(self, authorization: MCPServerAuthorization) -> None: ...

    async def record_catalog(
        self,
        tenant_id: str,
        server_id: str,
        catalog_hash: str,
        records: tuple[MCPToolCatalogRecord, ...],
    ) -> None: ...


class SecretSealer(Protocol):
    """Seal secrets at rest, bound to the row and field they belong to (ADR-0255)."""

    def seal(self, plaintext: str, context: str) -> str: ...

    def open(self, sealed: str, context: str) -> SecretValue: ...


class MCPOAuthGateway(Protocol):
    """Outbound sign-in traffic for owner-added servers; failures carry no upstream text."""

    async def probe(self, url: str) -> MCPProbeResult: ...

    async def register(
        self, metadata: MCPAuthorizationMetadata, redirect_uri: str
    ) -> MCPClientRegistration: ...

    async def exchange(
        self,
        metadata: MCPAuthorizationMetadata,
        registration: MCPClientRegistration,
        *,
        code: str,
        verifier: str,
        redirect_uri: str,
    ) -> MCPTokenGrant: ...

    async def refresh(
        self,
        metadata: MCPAuthorizationMetadata,
        registration: MCPClientRegistration,
        *,
        refresh_token: str,
    ) -> MCPTokenGrant: ...


class MCPAccessTokens(Protocol):
    """The runtime's source of a current access token for a signed-in server."""

    async def access_token(
        self, tenant_id: str, server_id: str, *, force_refresh: bool = False
    ) -> SecretValue:
        """Return a token, refreshing near expiry; PermissionError when signed out."""
        ...
