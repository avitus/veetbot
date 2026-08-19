"""MCP client factory contract."""

import pytest

from agent_core.adapters.mcp.scripted import ScriptedMCPClientFactory
from agent_core.domain.errors import MCPTransportError
from agent_core.domain.mcp import (
    MCPDiscovery,
    MCPRemoteTool,
    MCPServerConfig,
    MCPTransport,
    ScriptedMCPServer,
)


def _config(server_id: str) -> MCPServerConfig:
    return MCPServerConfig(
        tenant_id="tenant-a",
        server_id=server_id,
        transport=MCPTransport.STDIO,
        endpoint="server",
        operator_configured=True,
    )


def _script(name: str) -> ScriptedMCPServer:
    return ScriptedMCPServer(
        name=name,
        discovery=MCPDiscovery(
            tools=(MCPRemoteTool(name=f"{name}.lookup", input_schema={"type": "object"}),)
        ),
    )


async def test_factory_selects_the_script_named_by_the_server_id() -> None:
    factory = ScriptedMCPClientFactory({"docs": _script("docs"), "tickets": _script("tickets")})

    async with factory(_config("tickets"), None, {}) as client:
        discovery = await client.discover()

    assert [tool.name for tool in discovery.tools] == ["tickets.lookup"]


def test_unknown_server_id_is_a_transport_error() -> None:
    factory = ScriptedMCPClientFactory({"docs": ScriptedMCPServer(name="docs")})

    with pytest.raises(MCPTransportError):
        factory(_config("missing"), None, {})
