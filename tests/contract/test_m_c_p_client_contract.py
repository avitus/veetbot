"""Adapter-neutral MCP client lifecycle contract."""

from agent_core.adapters.mcp.scripted import ScriptedMCPClient
from agent_core.domain.mcp import MCPDiscovery, MCPRemoteTool, ScriptedMCPServer


async def test_mcp_client_enters_and_discovers_without_network() -> None:
    discovery = MCPDiscovery(tools=(MCPRemoteTool(name="search", input_schema={"type": "object"}),))
    client = ScriptedMCPClient(
        ScriptedMCPServer(name="docs", discovery=discovery),
        None,
        {},
    )
    async with client as entered:
        assert await entered.discover() == discovery
    assert client.entered is False


async def test_mcp_client_force_close_retires_transport_and_is_idempotent() -> None:
    """Forced closure stops transport ownership and permits subsequent harmless closure."""
    client = ScriptedMCPClient(ScriptedMCPServer(name="docs", discovery=MCPDiscovery()), None, {})
    await client.__aenter__()
    assert client.entered
    await client.force_close()
    assert not client.entered
    await client.force_close()
    await client.__aexit__(None, None, None)
    assert not client.entered
