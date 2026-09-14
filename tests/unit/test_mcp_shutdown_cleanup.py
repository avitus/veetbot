"""Runtime shutdown owns deferred MCP teardown until transport resources stop."""

import asyncio
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_core.adapters.mcp.scripted import ScriptedMCPClient
from agent_core.adapters.mcp.sdk import SDKMCPClient
from agent_core.bootstrap import build
from agent_core.domain.credentials import SecretValue
from agent_core.domain.errors import MCPTransportError
from agent_core.domain.mcp import MCPDiscovery, MCPServerConfig, MCPTransport, ScriptedMCPServer
from tests.gates.test_tool_m8 import _discovery, _server, _settings


@pytest.mark.parametrize(
    ("force", "preparation_failure", "failed_force"),
    [(False, False, False), (True, False, False), (True, True, False), (True, False, True)],
    ids=["graceful-drain", "forced-drain", "failed-preparation", "failed-force-retry"],
)
async def test_shutdown_drains_retained_client_cleanup(
    force: bool, preparation_failure: bool, failed_force: bool
) -> None:
    """A successful shutdown leaves neither retained exits nor entered transports."""
    allow_close, closed = asyncio.Event(), asyncio.Event()
    forced = False

    class Client(ScriptedMCPClient):
        async def discover(self) -> MCPDiscovery:
            """Exercise clients whose preparation fails before session ownership is stored."""
            if preparation_failure:
                raise MCPTransportError
            return await super().discover()

        async def __aexit__(self, *arguments: Any) -> None:
            """Hold teardown beyond the ordinary per-connection cleanup deadline."""
            try:
                await allow_close.wait()
                await super().__aexit__(*arguments)
            finally:
                closed.set()

        async def force_close(self) -> None:
            """Stop the actual fixture transport before releasing its pending teardown."""
            nonlocal forced, failed_force
            if failed_force:
                failed_force = False
                raise RuntimeError("injected abort failure")
            forced = True
            self.entered = False
            allow_close.set()

    client = Client(ScriptedMCPServer(name="deferred", discovery=_discovery()), None, {})

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> Client:
        """Expose the one tracked transport for this shutdown regression."""
        return client

    async with build(
        settings=_settings(), mcp_servers=(_server("deferred"),), mcp_client_factory=factory
    ) as app:
        app.mcp._connect_timeout_seconds = 0.01
        session = await app.sessions.create()
        if not preparation_failure:
            await app.mcp.release_session_transports(session)
        assert client.entered and app.mcp._preparation_cleanup_tasks
        if not force:
            asyncio.get_running_loop().call_later(0.02, allow_close.set)
            app.mcp._connect_timeout_seconds = 0.1
        try:
            if failed_force:
                with pytest.raises(MCPTransportError):
                    await app.mcp.close()
                assert client.entered and app.mcp._preparation_cleanup_tasks
            async with asyncio.timeout(1):
                await app.mcp.close()
            assert not client.entered
            assert closed.is_set()
            assert not app.mcp._preparation_cleanup_tasks
            assert forced is force
        finally:
            allow_close.set()
            await closed.wait()


async def test_runtime_force_close_stops_sdk_owner_before_cancelling_retained_exit() -> None:
    """The runtime retires real SDK resources even when the graceful exit caller stalls."""
    fixture = Path(__file__).resolve().parents[1] / "fixtures/mcp_stdio_environment_server.py"
    config = MCPServerConfig(
        tenant_id="local",
        server_id="deferred",
        transport=MCPTransport.STDIO,
        endpoint=shlex.join([sys.executable, str(fixture)]),
        operator_configured=True,
    )
    allow_close, exit_finished = asyncio.Event(), asyncio.Event()

    class Client(SDKMCPClient):
        async def __aexit__(self, *arguments: Any) -> None:
            """Delay the outer exit without replacing the transport owner or its scopes."""
            try:
                await allow_close.wait()
                await super().__aexit__(*arguments)
            finally:
                exit_finished.set()

    client = Client(config, None, {})

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> Client:
        """Return the actual SDK client whose shutdown ownership the test observes."""
        return client

    async with build(
        settings=_settings(), mcp_servers=(config,), mcp_client_factory=factory
    ) as app:
        session = await app.sessions.create()
        owner = client._owner
        assert owner is not None and not owner.done()
        app.mcp._connect_timeout_seconds = 0.05
        await app.mcp.release_session_transports(session)
        retained = tuple(app.mcp._preparation_cleanup_tasks)
        assert retained and not owner.done()
        try:
            async with asyncio.timeout(10):
                await app.mcp.close()
            assert owner.done()
            assert client._owner is None and client._stack is None and client._client is None
            assert exit_finished.is_set() and all(task.done() for task in retained)
            assert not app.mcp._preparation_cleanup_tasks
        finally:
            allow_close.set()
            await asyncio.gather(*retained, return_exceptions=True)


async def test_cancelled_shutdown_preserves_active_transport_ownership() -> None:
    """Cancellation cannot turn an incomplete runtime shutdown into a successful return."""
    from tests.unit.test_mcp_idle_lifecycle import Factory, context

    factory = Factory()
    async with build(
        settings=_settings(), mcp_servers=(_server("deferred"),), mcp_client_factory=factory
    ) as app:
        session = await app.sessions.create()
        spec = app.mcp._registry.get("mcp.deferred.echo", tenant_id="local").spec
        factory.release.clear()
        calling = asyncio.create_task(app.mcp.call_tool(context(app, session), spec, "echo", {}))
        await factory.started.wait()
        closing = asyncio.create_task(app.mcp.close())
        try:
            async with asyncio.timeout(1):
                while not app.mcp._lock(session).locked():
                    await asyncio.sleep(0)
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
            assert session in app.mcp._sessions and factory.clients[0].entered
        finally:
            factory.release.set()
            await asyncio.gather(calling, closing, return_exceptions=True)
            await app.mcp.close()
        assert not factory.clients[0].entered
