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
