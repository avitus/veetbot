"""A chat's MCP pins commit in one PostgreSQL transaction, or not at all (ADR-0131)."""

from typing import Any

import pytest

from agent_core.adapters.mcp.scripted import ScriptedMCPClient
from agent_core.adapters.persistence.repositories import PostgresEventRepository
from agent_core.bootstrap import build
from agent_core.domain.credentials import SecretValue
from agent_core.domain.events import NewEvent
from agent_core.domain.mcp import MCPServerConfig, ScriptedMCPServer
from agent_core.domain.persistence import WorkerLease
from tests.gates.test_tool_m8 import _discovery, _server
from tests.integration.m2_support import database_settings

pytestmark = pytest.mark.integration


def _client(
    config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
) -> ScriptedMCPClient:
    server = ScriptedMCPServer(name=config.server_id, discovery=_discovery())
    return ScriptedMCPClient(server, credential, environment)


async def test_a_failed_pin_write_commits_none_of_the_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = PostgresEventRepository.append

    async def refuse_beta(
        self: PostgresEventRepository, event: NewEvent, *, lease: WorkerLease | None = None
    ) -> Any:
        if event.event_type == "mcp.server.pinned" and event.payload.get("server_id") == "beta":
            raise RuntimeError("the database refused the second pin")
        return await original(self, event, lease=lease)

    async with build(
        settings=database_settings(),
        storage="postgres",
        mcp_servers=(_server("alpha"), _server("beta")),
        mcp_client_factory=_client,
    ) as app:
        await app.sessions.create()
        session = await app.sessions.create()
        await app.mcp.close_session(session)
        monkeypatch.setattr(PostgresEventRepository, "append", refuse_beta)

        with pytest.raises(RuntimeError, match="refused the second pin"):
            await app.mcp.prepare(session, app.principal)

        monkeypatch.setattr(PostgresEventRepository, "append", original)
        async with app.uow_factory() as uow:
            events = await uow.events.list_after(session, 0, app.principal)

    pinned = [
        event.payload["server_id"] for event in events if event.event_type == "mcp.server.pinned"
    ]
    # Only the two pins from creating the chat; alpha's second pin rolled back with beta's.
    assert pinned == ["alpha", "beta"]
