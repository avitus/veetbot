"""Application-only Bland operations through the credential-confined MCP client port."""

import asyncio
from typing import Any

from agent_core.domain.credentials import CredentialRef
from agent_core.domain.mcp import MCPCallResult
from agent_core.mcp.configuration import build_stdio_environment, calling_server_configs
from agent_core.ports.credentials import CredentialResolver
from agent_core.ports.mcp import MCPClientFactory


class BlandCallProvider:
    def __init__(
        self, tenant_id: str, clients: MCPClientFactory, credentials: CredentialResolver
    ) -> None:
        self.configs = {config.server_id: config for config in calling_server_configs(tenant_id)}
        self.clients = clients
        self.credentials = credentials

    async def __call__(self, server: str, name: str, arguments: dict[str, Any]) -> MCPCallResult:
        if (server, name) not in {
            ("bland_read", "provider_list_calls"),
            ("bland_read", "provider_get_call"),
            ("bland_call", "provider_stop_call"),
        }:
            raise ValueError("calling worker operation is not permitted")
        config = self.configs[server]
        credential = await self.credentials.resolve(CredentialRef(server))
        async with (
            asyncio.timeout(25),
            self.clients(config, credential, build_stdio_environment(config, credential)) as client,
        ):
            return await client.call_tool(name, arguments)
