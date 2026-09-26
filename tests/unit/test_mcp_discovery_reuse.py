"""New chats pin remembered MCP discovery and start servers on first use (ADR-0131)."""

from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.mcp.scripted import ScriptedMCPClient
from agent_core.bootstrap import Composition, build
from agent_core.domain.credentials import SecretValue
from agent_core.domain.errors import MCPTransportError
from agent_core.domain.events import EventEnvelope
from agent_core.domain.mcp import (
    MCPCallResult,
    MCPDiscovery,
    MCPServerConfig,
    MCPTransport,
    ScriptedMCPServer,
)
from agent_core.domain.tools import ToolExecutionContext
from tests.contract.support import tool_context
from tests.gates.test_tool_m8 import _discovery, _preparation_settings, _server, _settings

START = datetime(2026, 9, 25, tzinfo=UTC)


class _Factory:
    """Count transport starts per server, with controllable discovery, failure and duration."""

    def __init__(self) -> None:
        self.started: Counter[str] = Counter()
        self.clients: list[ScriptedMCPClient] = []
        self.discovery = _discovery()
        self.failing: set[str] = set()
        self.on_discover: Callable[[], None] | None = None

    def __call__(
        self, config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        self.started[config.server_id] += 1
        factory = self
        failing = config.server_id in self.failing

        class Client(ScriptedMCPClient):
            async def discover(self) -> MCPDiscovery:
                if factory.on_discover is not None:
                    factory.on_discover()
                if failing:
                    raise MCPTransportError
                return await super().discover()

            async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPCallResult:
                return MCPCallResult(content=("ok",))

        client = Client(
            ScriptedMCPServer(name=config.server_id, discovery=self.discovery),
            credential,
            environment,
        )
        self.clients.append(client)
        return client


def _context(app: Composition, session_id: UUID) -> ToolExecutionContext:
    return replace(
        tool_context(),
        session_id=session_id,
        tenant_id=app.principal.tenant_id,
        principal=app.principal,
    )


async def _events(app: Composition, session_id: UUID) -> list[EventEnvelope]:
    async with app.uow_factory() as uow:
        return await uow.events.list_after(session_id, 0, app.principal)


def _payloads(events: list[EventEnvelope], event_type: str) -> list[dict[str, Any]]:
    return [event.payload for event in events if event.event_type == event_type]


def _advance_during_discovery(app: Composition, factory: _Factory, milliseconds: int) -> None:
    clock = cast(FixedClock, app.clock)
    factory.on_discover = lambda: clock.advance(timedelta(milliseconds=milliseconds))


async def test_a_second_chat_pins_the_remembered_discovery_without_starting_servers() -> None:
    factory = _Factory()
    async with build(
        settings=_settings(),
        fixed_clock_at=START,
        mcp_servers=(_server("alpha"), _server("beta")),
        mcp_client_factory=factory,
    ) as app:
        first = await app.sessions.create()
        assert factory.started == Counter({"alpha": 1, "beta": 1})
        live = _payloads(await _events(app, first), "mcp.server.connected")

        second = await app.sessions.create()

        assert factory.started == Counter({"alpha": 1, "beta": 1})
        assert set(app.mcp._sessions[second]) == {"alpha", "beta"}
        assert all(connection.client is None for connection in app.mcp._sessions[second].values())
        assert app.mcp._registry.get("mcp.alpha.echo", tenant_id="local")
        events = await _events(app, second)
        assert not _payloads(events, "mcp.server.connected")
        assert _payloads(events, "mcp.server.pinned") == [
            {
                "server_id": server["server_id"],
                "transport": "stdio",
                "catalog_hash": server["catalog_hash"],
                "tool_count": 1,
                "rejected": [],
                "conflicts": [],
            }
            for server in live
        ]


async def test_a_live_handshake_is_timed_and_names_its_transport() -> None:
    factory = _Factory()
    async with build(
        settings=_settings(),
        fixed_clock_at=START,
        mcp_servers=(_server("alpha"),),
        mcp_client_factory=factory,
    ) as app:
        _advance_during_discovery(app, factory, 1500)
        session = await app.sessions.create()

        [connected] = _payloads(await _events(app, session), "mcp.server.connected")
        assert connected["transport"] == "stdio"
        assert connected["duration_ms"] == 1500


async def test_the_first_call_on_a_reused_pin_starts_its_server_and_records_the_handshake() -> None:
    factory = _Factory()
    async with build(
        settings=_settings(),
        fixed_clock_at=START,
        mcp_servers=(_server("alpha"), _server("beta")),
        mcp_client_factory=factory,
    ) as app:
        await app.sessions.create()
        second = await app.sessions.create()
        spec = app.mcp._registry.get("mcp.alpha.echo", tenant_id="local").spec
        _advance_during_discovery(app, factory, 700)

        result = await app.mcp.call_tool(_context(app, second), spec, "echo", {"value": "x"})

        assert result.ok
        assert factory.started == Counter({"alpha": 2, "beta": 1})
        connected = _payloads(await _events(app, second), "mcp.server.connected")
        assert [(item["server_id"], item["duration_ms"]) for item in connected] == [("alpha", 700)]


async def test_a_reconnection_that_finds_a_changed_catalog_withdraws_and_updates_the_memory() -> (
    None
):
    factory = _Factory()
    async with build(
        settings=_settings(),
        fixed_clock_at=START,
        mcp_servers=(_server("alpha"),),
        mcp_client_factory=factory,
    ) as app:
        await app.sessions.create()
        factory.discovery = MCPDiscovery()
        second = await app.sessions.create()
        spec = app.mcp._registry.get("mcp.alpha.echo", tenant_id="local").spec

        result = await app.mcp.call_tool(_context(app, second), spec, "echo", {})

        assert result.failure is not None
        assert result.failure.reason_code == "tool.withdrawn"
        assert _payloads(await _events(app, second), "mcp.catalog.changed")
        third = await app.sessions.create()
        assert factory.started == Counter({"alpha": 2})
        [pinned] = _payloads(await _events(app, third), "mcp.server.pinned")
        assert pinned["tool_count"] == 0


async def test_a_failed_reconnection_forgets_the_remembered_discovery() -> None:
    factory = _Factory()
    async with build(
        settings=_settings(),
        fixed_clock_at=START,
        mcp_servers=(_server("alpha"),),
        mcp_client_factory=factory,
    ) as app:
        await app.sessions.create()
        factory.failing.add("alpha")
        second = await app.sessions.create()
        spec = app.mcp._registry.get("mcp.alpha.echo", tenant_id="local").spec

        result = await app.mcp.call_tool(_context(app, second), spec, "echo", {})

        assert result.failure is not None
        assert result.failure.reason_code == "tool.server_unreachable"
        third = await app.sessions.create()
        assert factory.started == Counter({"alpha": 3})
        events = await _events(app, third)
        assert _payloads(events, "mcp.server.disconnected")
        assert not _payloads(events, "mcp.server.pinned")


async def test_preparation_that_names_its_servers_stays_live() -> None:
    factory = _Factory()
    async with build(
        settings=_settings(),
        fixed_clock_at=START,
        mcp_servers=(_server("alpha"),),
        mcp_client_factory=factory,
    ) as app:
        await app.sessions.create()

        await app.mcp.prepare(uuid4(), app.principal, server_ids=frozenset({"alpha"}))

        assert factory.started == Counter({"alpha": 2})


@pytest.mark.parametrize(
    ("transport", "starts_after_an_hour"),
    [(MCPTransport.STDIO, 1), (MCPTransport.HTTP, 2)],
)
async def test_only_a_tenant_http_discovery_expires(
    transport: MCPTransport, starts_after_an_hour: int, tmp_path: Path
) -> None:
    factory = _Factory()
    async with build(
        settings=_preparation_settings(tmp_path),
        fixed_clock_at=START,
        mcp_servers=(_server("remote", transport=transport),),
        mcp_client_factory=factory,
    ) as app:
        await app.sessions.create()
        cast(FixedClock, app.clock).advance(timedelta(seconds=3599))
        await app.sessions.create()
        assert factory.started == Counter({"remote": 1})

        cast(FixedClock, app.clock).advance(timedelta(seconds=2))
        await app.sessions.create()

        assert factory.started == Counter({"remote": starts_after_an_hour})


async def test_the_warm_up_discovers_each_server_once_and_records_nothing() -> None:
    factory = _Factory()
    async with build(
        settings=_settings(),
        fixed_clock_at=START,
        mcp_servers=(_server("alpha"), _server("beta")),
        mcp_client_factory=factory,
    ) as app:
        await app.start_mcp_discovery_warmup()

        assert factory.started == Counter({"alpha": 1, "beta": 1})
        assert not any(client.entered for client in factory.clients)
        assert app.mcp._sessions == {}
        session = await app.sessions.create()
        assert factory.started == Counter({"alpha": 1, "beta": 1})
        assert len(_payloads(await _events(app, session), "mcp.server.pinned")) == 2


async def test_a_failed_warm_up_leaves_the_server_to_live_discovery() -> None:
    factory = _Factory()
    factory.failing.add("alpha")
    async with build(
        settings=_settings(),
        fixed_clock_at=START,
        mcp_servers=(_server("alpha"), _server("beta")),
        mcp_client_factory=factory,
    ) as app:
        await app.start_mcp_discovery_warmup()
        factory.failing.clear()

        session = await app.sessions.create()

        assert factory.started == Counter({"alpha": 2, "beta": 1})
        events = await _events(app, session)
        assert [item["server_id"] for item in _payloads(events, "mcp.server.connected")] == [
            "alpha"
        ]
        assert [item["server_id"] for item in _payloads(events, "mcp.server.pinned")] == ["beta"]
