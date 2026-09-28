"""A chat's MCP pins commit in one PostgreSQL transaction, or not at all (ADR-0131)."""

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

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
    """Provide deterministic discovery without opening a real MCP transport."""
    server = ScriptedMCPServer(name=config.server_id, discovery=_discovery())
    return ScriptedMCPClient(server, credential, environment)


async def test_a_failed_pin_write_commits_none_of_the_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second server's pin failure rolls back the first server's pin too."""
    original = PostgresEventRepository.append

    async def refuse_beta(
        self: PostgresEventRepository, event: NewEvent, *, lease: WorkerLease | None = None
    ) -> Any:
        """Inject a database failure after alpha's pin enters the transaction."""
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


@pytest.mark.parametrize("stage", ["append", "commit"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_activation_preserves_its_batch_for_exactly_once_retry(
    stage: str, cancelled: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An activation rollback retains every pending event until a successful retry."""
    original = PostgresEventRepository.append
    error = asyncio.CancelledError() if cancelled else RuntimeError("injected transaction failure")

    async def refuse_beta(
        self: PostgresEventRepository, event: NewEvent, *, lease: WorkerLease | None = None
    ) -> Any:
        """Fail after the first server's event has entered the uncommitted transaction."""
        if event.event_type.startswith("mcp.") and event.payload.get("server_id") == "beta":
            raise error
        return await original(self, event, lease=lease)

    async def refuse_commit(self: AsyncSession) -> None:
        """Fail at the commit boundary after both events were appended."""
        del self
        raise error

    async with build(
        settings=database_settings(),
        storage="postgres",
        mcp_servers=(_server("alpha"), _server("beta")),
        mcp_client_factory=_client,
    ) as app:
        async with app.uow_factory() as uow:
            session = await app.sessions.create_in(uow)
        pending = list(app.mcp._pending_events[session])
        assert len(pending) == 2

        with monkeypatch.context() as patch:
            if stage == "append":
                patch.setattr(PostgresEventRepository, "append", refuse_beta)
            else:
                patch.setattr(AsyncSession, "commit", refuse_commit)
            with pytest.raises(type(error)):
                await app.sessions.activate(session)

        assert app.mcp._pending_events.get(session) == pending
        assert session in app.mcp._deferred_events
        async with app.uow_factory() as uow:
            rolled_back = await uow.events.list_after(session, 0, app.principal)
        assert not [event for event in rolled_back if event.event_type.startswith("mcp.")]

        await app.sessions.activate(session)
        await app.sessions.activate(session)

        assert session not in app.mcp._pending_events
        assert session not in app.mcp._deferred_events
        async with app.uow_factory() as uow:
            events = await uow.events.list_after(session, 0, app.principal)
        assert [
            (event.event_type, event.payload)
            for event in events
            if event.event_type.startswith("mcp.")
        ] == pending


async def test_concurrent_activation_waits_for_the_same_batch_to_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second activation cannot report success while the first batch is uncommitted."""
    original = PostgresEventRepository.append
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_alpha(
        self: PostgresEventRepository, event: NewEvent, *, lease: WorkerLease | None = None
    ) -> Any:
        """Pause activation at its first event so a second caller overlaps it."""
        if event.event_type.startswith("mcp.") and event.payload.get("server_id") == "alpha":
            entered.set()
            await release.wait()
        return await original(self, event, lease=lease)

    async with build(
        settings=database_settings(),
        storage="postgres",
        mcp_servers=(_server("alpha"), _server("beta")),
        mcp_client_factory=_client,
    ) as app:
        async with app.uow_factory() as uow:
            session = await app.sessions.create_in(uow)
        pending = list(app.mcp._pending_events[session])
        monkeypatch.setattr(PostgresEventRepository, "append", hold_alpha)
        first = asyncio.create_task(app.sessions.activate(session))
        second: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            second = asyncio.create_task(app.sessions.activate(session))
            await asyncio.sleep(0)
            assert not second.done()
        finally:
            release.set()
            await first
            if second is not None:
                await second

        async with app.uow_factory() as uow:
            events = await uow.events.list_after(session, 0, app.principal)
        assert [
            (event.event_type, event.payload)
            for event in events
            if event.event_type.startswith("mcp.")
        ] == pending
