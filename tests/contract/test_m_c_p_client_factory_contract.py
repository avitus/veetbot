"""MCP client factory contract."""

from agent_core.adapters.mcp.scripted import ScriptedMCPClientFactory
from agent_core.domain.mcp import MCPServerConfig, MCPTransport, ScriptedMCPServer


def test_mcp_client_factory_selects_the_configured_server() -> None:
    factory = ScriptedMCPClientFactory({"docs": ScriptedMCPServer(name="docs")})
    client = factory(
        MCPServerConfig(
            tenant_id="tenant-a",
            server_id="docs",
            transport=MCPTransport.STDIO,
            endpoint="server",
            operator_configured=True,
        ),
        None,
        {},
    )
    assert factory.created == [client]
