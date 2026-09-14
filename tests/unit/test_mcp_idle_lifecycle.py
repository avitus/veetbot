"""MCP transport leases end without discarding a session's pinned advertisement."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.mcp.scripted import ScriptedMCPClient, ScriptedMCPClientFactory
from agent_core.bootstrap import Composition, build
from agent_core.domain.credentials import SecretValue
from agent_core.domain.errors import MCPUnavailableError, NotFoundError
from agent_core.domain.mcp import (
    MCPCallResult,
    MCPDiscovery,
    MCPRemoteTool,
    MCPServerConfig,
    ScriptedMCPResponse,
    ScriptedMCPServer,
)
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.tools import ToolExecutionContext
from tests.contract.support import tool_context
from tests.gates.test_tool_m8 import _discovery, _RotatingCredentials, _server, _settings


class Factory:
    """Track actual connection lifetime while returning a harmless read result."""

    def __init__(self) -> None:
        """Initialize client tracking, pinned discovery, and controllable call barriers."""
        self.clients: list[ScriptedMCPClient] = []
        self.discovery = _discovery()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    def __call__(
        self, config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        """Create a tracked transport whose calls can be paused independently of discovery."""
        factory = self

        class Client(ScriptedMCPClient):
            async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPCallResult:
                """Signal invocation entry and wait for the test to release its transport lease."""
                factory.started.set()
                await factory.release.wait()
                return MCPCallResult(content=("ok",))

        client = Client(
            ScriptedMCPServer(name=config.server_id, discovery=self.discovery),
            credential,
            environment,
        )
        self.clients.append(client)
        return client


def context(app: Composition, session_id: UUID) -> ToolExecutionContext:
    """Bind a tool invocation context to the composition principal and selected session."""
    return replace(
        tool_context(),
        session_id=session_id,
        tenant_id=app.principal.tenant_id,
        principal=app.principal,
    )


async def test_terminal_runs_release_transports_without_dropping_tool_pins() -> None:
    """Check repeated completed runs close their clients while retaining session tool pins."""
    factory = Factory()
    script = FakeModelScript(turns=[ScriptedTurn(text="done")], on_exhausted="repeat_last")
    async with build(
        settings=_settings(),
        fixed_clock_at=datetime(2026, 9, 1, tzinfo=UTC),
        mcp_servers=(_server("idle"),),
        mcp_client_factory=factory,
        script=script,
    ) as app:
        for _ in range(3):
            run = await app.runs.get(await app.runs.submit("hello"))
            assert run.status.value == "COMPLETED"
            assert not any(client.entered for client in factory.clients)
            assert app.mcp._registry.get("mcp.idle.echo", tenant_id="local")
            assert run.session_id in app.mcp._prepared


async def test_idle_release_reconnects_without_repinning_or_reviving_withdrawn_tools() -> None:
    """Keep the original tool pin when idle reconnection discovers that its tool was removed."""
    factory = Factory()
    async with build(
        settings=_settings(),
        fixed_clock_at=datetime(2026, 9, 1, tzinfo=UTC),
        mcp_servers=(_server("idle"),),
        mcp_client_factory=factory,
    ) as app:
        session = await app.sessions.create()
        spec = app.mcp._registry.get("mcp.idle.echo", tenant_id="local").spec
        cast(FixedClock, app.clock).advance(timedelta(seconds=901))
        await app.mcp.sweep_idle()
        assert not factory.clients[0].entered
        factory.discovery = MCPDiscovery()
        result = await app.mcp.call_tool(context(app, session), spec, "echo", {})
        assert not result.ok
        assert result.failure is not None
        assert result.failure.reason_code == "tool.withdrawn"
        assert app.mcp._registry.get("mcp.idle.echo", tenant_id="local").spec == spec


async def test_idle_sweep_does_not_close_an_active_call() -> None:
    """Protect an in-flight call and restart its idle timeout when execution finishes."""
    factory = Factory()
    async with build(
        settings=_settings(),
        fixed_clock_at=datetime(2026, 9, 1, tzinfo=UTC),
        mcp_servers=(_server("idle"),),
        mcp_client_factory=factory,
    ) as app:
        session = await app.sessions.create()
        spec = app.mcp._registry.get("mcp.idle.echo", tenant_id="local").spec
        factory.release.clear()
        pending = asyncio.create_task(app.mcp.call_tool(context(app, session), spec, "echo", {}))
        try:
            await factory.started.wait()
            cast(FixedClock, app.clock).advance(timedelta(seconds=901))
            await app.mcp.sweep_idle()
            assert factory.clients[0].entered
        finally:
            factory.release.set()
            await pending
        await app.mcp.sweep_idle()
        assert factory.clients[0].entered
        cast(FixedClock, app.clock).advance(timedelta(seconds=901))
        await app.mcp.sweep_idle()
        assert not factory.clients[0].entered


async def test_closed_session_is_reaped_in_the_worker_that_owns_its_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observe durable API closure and reclaim the transports owned by another runtime."""
    factory = Factory()
    async with build(
        settings=_settings(),
        fixed_clock_at=datetime(2026, 9, 1, tzinfo=UTC),
        mcp_servers=(_server("idle"),),
        mcp_client_factory=factory,
    ) as app:
        session = await app.sessions.create()
        # The API commits closure, but its local runtime cannot close another process.
        monkeypatch.setattr(app.services.sessions, "_close_session", None)
        await app.services.sessions.close(app.principal, session)
        assert factory.clients[0].entered
        await app.mcp.sweep_idle()
        assert not factory.clients[0].entered
        assert session not in app.mcp._prepared


async def test_idle_timer_uses_configured_timeout_without_foreground_activity(
    tmp_path: Path,
) -> None:
    """Apply the configured idle timeout through maintenance without another user request."""
    overlay = tmp_path / "tools" / "limits.yaml"
    overlay.parent.mkdir()
    overlay.write_text("mcp:\n  idle_timeout_seconds: 1\n")
    factory = Factory()
    async with build(
        settings=replace(_settings(), config_dir=tmp_path),
        mcp_servers=(_server("idle"),),
        mcp_client_factory=factory,
    ) as app:
        await app.sessions.create()
        async with asyncio.timeout(3):
            while factory.clients[0].entered:
                await asyncio.sleep(0.01)


@pytest.mark.parametrize("changed_schema", [False, True])
async def test_reconnect_preserves_unchanged_tool_and_rejects_changed_schema(
    changed_schema: bool,
) -> None:
    """Admit unchanged pinned tools while rejecting changed schemas and unpinned additions."""
    factory = Factory()
    async with build(
        settings=_settings(), mcp_servers=(_server("idle"),), mcp_client_factory=factory
    ) as app:
        session = await app.sessions.create()
        spec = app.mcp._registry.get("mcp.idle.echo", tenant_id="local").spec
        await app.mcp.release_session_transports(session)
        echo = _discovery().tools[0]
        if changed_schema:
            echo = echo.model_copy(update={"input_schema": {"type": "object", "properties": {}}})
        factory.discovery = MCPDiscovery(
            tools=(echo, MCPRemoteTool(name="added", description="New tool", input_schema={}))
        )
        result = await app.mcp.call_tool(context(app, session), spec, "echo", {})
        assert result.ok is not changed_schema
        if changed_schema:
            assert result.failure is not None
            assert result.failure.reason_code == "tool.withdrawn"
        assert factory.clients[-1].entered
        assert app.mcp._registry.get("mcp.idle.echo", tenant_id="local").spec == spec
        with pytest.raises(NotFoundError):
            app.mcp._registry.get("mcp.idle.added", tenant_id="local")


async def test_reconnect_does_not_reset_session_authentication_budget() -> None:
    """Retain the one-refresh authentication limit and terminal denial across reconnections."""
    factory = ScriptedMCPClientFactory(
        {
            "idle": ScriptedMCPServer(
                name="idle",
                discovery=_discovery(),
                responses=(
                    ScriptedMCPResponse(name="echo", outcome="unauthorized"),
                    ScriptedMCPResponse(name="echo", result=MCPCallResult(content=("retried",))),
                ),
            )
        }
    )
    async with build(
        settings=_settings(),
        mcp_servers=(_server("idle", credential_ref="idle-ref"),),
        mcp_client_factory=factory,
        credential_resolver=_RotatingCredentials(),
    ) as app:
        session = await app.sessions.create()
        spec = app.mcp._registry.get("mcp.idle.echo", tenant_id="local").spec
        assert (await app.mcp.call_tool(context(app, session), spec, "echo", {})).ok
        await app.mcp.release_session_transports(session)
        result = await app.mcp.call_tool(context(app, session), spec, "echo", {})
        assert result.failure is not None
        assert result.failure.reason_code == "tool.server_unauthorized"
        assert [client.reauthentication_count for client in factory.created] == [1, 0]
        assert not any(client.entered for client in factory.created)
        await app.mcp.release_session_transports(session)
        await app.mcp.call_tool(context(app, session), spec, "echo", {})
        assert len(factory.created) == 2


async def test_shutdown_drains_a_session_still_being_prepared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drain interrupted session discovery without leaving clients or maintenance running."""
    factory = Factory()
    started, release = asyncio.Event(), asyncio.Event()
    discover = ScriptedMCPClient.discover

    async def blocked_discovery(client: ScriptedMCPClient) -> MCPDiscovery:
        """Hold discovery until the test has started concurrent runtime shutdown."""
        started.set()
        await release.wait()
        return await discover(client)

    monkeypatch.setattr(ScriptedMCPClient, "discover", blocked_discovery)
    async with build(
        settings=_settings(), mcp_servers=(_server("idle"),), mcp_client_factory=factory
    ) as app:
        opening = asyncio.create_task(app.sessions.create())
        await started.wait()
        closing = asyncio.create_task(app.mcp.close())
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(MCPUnavailableError):
            await opening
        await closing
        assert not any(client.entered for client in factory.clients)
        assert app.mcp._maintenance_task is None


async def test_transport_cleanup_failure_does_not_retain_sibling_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Continue reclaiming sibling transports when one client reports a cleanup failure."""
    factory = Factory()
    close = ScriptedMCPClient.__aexit__

    async def faulty_close(self: ScriptedMCPClient, *args: Any) -> None:
        """Close normally, then inject a cleanup error for only the first tracked client."""
        await close(self, *args)
        if self is factory.clients[0]:
            raise RuntimeError("fixture cleanup failure")

    monkeypatch.setattr(ScriptedMCPClient, "__aexit__", faulty_close)
    async with build(
        settings=_settings(),
        mcp_servers=(_server("first"), _server("second")),
        mcp_client_factory=factory,
    ) as app:
        session = await app.sessions.create()
        await app.mcp.release_session_transports(session)
        assert not any(client.entered for client in factory.clients)


async def test_cancelled_session_close_preserves_ownership_until_active_call_finishes() -> None:
    """A cancelled lease wait must leave live transports discoverable by shutdown."""
    factory = Factory()
    async with build(
        settings=_settings(), mcp_servers=(_server("idle"),), mcp_client_factory=factory
    ) as app:
        session = await app.sessions.create()
        spec = app.mcp._registry.get("mcp.idle.echo", tenant_id="local").spec
        factory.release.clear()
        pending = asyncio.create_task(app.mcp.call_tool(context(app, session), spec, "echo", {}))
        try:
            await factory.started.wait()
            closing = asyncio.create_task(app.mcp.close_session(session))
            await asyncio.sleep(0)
            assert not closing.done()
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
            assert factory.clients[0].entered
        finally:
            factory.release.set()
            await pending
        await app.mcp.close()
        assert not any(client.entered for client in factory.clients)
