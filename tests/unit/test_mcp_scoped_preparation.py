"""Typed work prepares only its MCP servers; full preparation adds the rest (ADR-0104)."""

from collections import Counter
from uuid import uuid4

import pytest

from agent_core.adapters.mcp.scripted import ScriptedMCPClient
from agent_core.bootstrap import build
from agent_core.domain.credentials import SecretValue
from agent_core.domain.errors import NotFoundError
from agent_core.domain.mcp import MCPDiscovery, MCPServerConfig, ScriptedMCPServer
from tests.gates.test_tool_m8 import _discovery, _server, _settings


class _Factory:
    """Count transport starts per server; one server can be made to fail discovery."""

    def __init__(self) -> None:
        self.started: Counter[str] = Counter()
        self.failing: set[str] = set()

    def __call__(
        self, config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        self.started[config.server_id] += 1
        failing = config.server_id in self.failing

        class Client(ScriptedMCPClient):
            async def discover(self) -> MCPDiscovery:
                if failing:
                    raise RuntimeError("discovery failed")
                return await super().discover()

        return Client(
            ScriptedMCPServer(name=config.server_id, discovery=_discovery()),
            credential,
            environment,
        )


def _servers() -> tuple[MCPServerConfig, ...]:
    return (_server("alpha"), _server("beta"), _server("gamma"))


async def test_named_servers_start_alone_and_full_preparation_adds_only_the_rest() -> None:
    factory = _Factory()
    async with build(
        settings=_settings(), mcp_servers=_servers(), mcp_client_factory=factory
    ) as app:
        session_id = uuid4()
        await app.mcp.prepare(session_id, app.principal, server_ids=frozenset({"beta"}))
        assert factory.started == Counter({"beta": 1})
        assert app.mcp._registry.get("mcp.beta.echo", tenant_id="local")
        with pytest.raises(NotFoundError):
            app.mcp._registry.get("mcp.alpha.echo", tenant_id="local")

        await app.mcp.prepare(session_id, app.principal, server_ids=frozenset({"beta"}))
        assert factory.started == Counter({"beta": 1})

        await app.mcp.prepare(session_id, app.principal)
        assert factory.started == Counter({"alpha": 1, "beta": 1, "gamma": 1})
        assert set(app.mcp._sessions[session_id]) == {"alpha", "beta", "gamma"}

        await app.mcp.prepare(session_id, app.principal, server_ids=frozenset({"alpha"}))
        await app.mcp.prepare(session_id, app.principal)
        assert factory.started == Counter({"alpha": 1, "beta": 1, "gamma": 1})


async def test_failed_scoped_preparation_keeps_servers_already_prepared() -> None:
    factory = _Factory()
    factory.failing.add("gamma")
    async with build(
        settings=_settings(), mcp_servers=_servers(), mcp_client_factory=factory
    ) as app:
        session_id = uuid4()
        await app.mcp.prepare(session_id, app.principal, server_ids=frozenset({"alpha"}))
        with pytest.raises(RuntimeError, match="discovery failed"):
            await app.mcp.prepare(session_id, app.principal, server_ids=frozenset({"gamma"}))
        assert app.mcp._registry.get("mcp.alpha.echo", tenant_id="local")
        assert set(app.mcp._sessions[session_id]) == {"alpha"}

        factory.failing.clear()
        await app.mcp.prepare(session_id, app.principal, server_ids=frozenset({"gamma"}))
        assert factory.started == Counter({"alpha": 1, "gamma": 2})
        assert set(app.mcp._sessions[session_id]) == {"alpha", "gamma"}


async def test_closing_a_scoped_session_forgets_its_prepared_servers() -> None:
    factory = _Factory()
    async with build(
        settings=_settings(), mcp_servers=_servers(), mcp_client_factory=factory
    ) as app:
        session_id = uuid4()
        await app.mcp.prepare(session_id, app.principal, server_ids=frozenset({"alpha"}))
        await app.mcp.close_session(session_id)
        with pytest.raises(NotFoundError):
            app.mcp._registry.get("mcp.alpha.echo", tenant_id="local")
        await app.mcp.prepare(session_id, app.principal, server_ids=frozenset({"alpha"}))
        assert factory.started == Counter({"alpha": 2})
