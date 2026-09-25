"""Real stdio SDK connections keep AnyIO context ownership within one task."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
from mcp import Client as ProtocolClient
from mcp.client import stdio as mcp_stdio

from agent_core.adapters.mcp.sdk import SDKMCPClient
from agent_core.bootstrap import build
from agent_core.domain.credentials import SecretValue
from agent_core.domain.errors import MCPTransportError
from agent_core.domain.mcp import MCPAuthScheme, MCPServerConfig, MCPTransport
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.runs import RunStatus
from agent_core.mcp.configuration import build_stdio_environment
from tests.integration.m2_support import memory_settings


def stdio_client() -> SDKMCPClient:
    """Use the actual SDK transport with only an isolated fixture credential."""
    server = Path(__file__).resolve().parents[1] / "fixtures/mcp_stdio_environment_server.py"
    config = MCPServerConfig(
        tenant_id="local",
        server_id="lifecycle",
        transport=MCPTransport.STDIO,
        endpoint=shlex.join([sys.executable, str(server)]),
        operator_configured=True,
        auth_scheme=MCPAuthScheme.ENV,
        auth_name="MCP_TOKEN",
        credential_ref="fixture-token",
    )
    credential = SecretValue("initial-fixture-value")
    return SDKMCPClient(config, credential, build_stdio_environment(config, credential))


async def test_completed_runs_release_real_sdk_transports_without_accumulation() -> None:
    """Completed runs retire their actual stdio contexts before another session opens."""
    server = Path(__file__).resolve().parents[1] / "fixtures/mcp_stdio_environment_server.py"
    configs = tuple(
        MCPServerConfig(
            tenant_id="local",
            server_id=f"lifecycle_{index}",
            transport=MCPTransport.STDIO,
            endpoint=shlex.join([sys.executable, str(server)]),
            operator_configured=True,
        )
        for index in range(2)
    )
    clients: list[SDKMCPClient] = []
    owners: list[asyncio.Task[BaseException | None]] = []

    class TrackedClient(SDKMCPClient):
        async def __aenter__(self) -> TrackedClient:
            """Observe real, running lifetime tasks before completion clears their handles."""
            await super().__aenter__()
            assert self._owner is not None and not self._owner.done()
            assert self._client is not None
            owners.append(self._owner)
            return self

    def factory(
        config: MCPServerConfig,
        credential: SecretValue | None,
        environment: dict[str, str],
    ) -> TrackedClient:
        """Track actual SDK clients without substituting their transports or cleanup."""
        client = TrackedClient(config, credential, environment)
        clients.append(client)
        return client

    async with build(
        settings=memory_settings(),
        script=FakeModelScript(
            turns=[ScriptedTurn(text="Hello.")],
            on_exhausted="repeat_last",
        ),
        mcp_servers=configs,
        mcp_client_factory=factory,
        sequential_ids=True,
    ) as composition:
        for index in range(3):
            # Forget remembered catalogs (ADR-0131) so every session starts real transports.
            composition.mcp._discoveries.clear()
            run_id = await composition.runs.submit("Return a short greeting.")
            run = await composition.runs.get(run_id)
            assert run.status is RunStatus.COMPLETED
            assert len(clients) == len(owners) == 2 * (index + 1)
            assert all(owner.done() for owner in owners)
            assert all(client._owner is None for client in clients)
            assert all(client._client is None and client._stack is None for client in clients)


async def test_real_sdk_connection_survives_preparation_task_and_closes_in_another() -> None:
    """Discovery workers may finish before the worker or API closes their connection."""
    client = stdio_client()

    async def prepare() -> None:
        """Match the short-lived parallel preparation task used by MCPRuntime."""
        await client.__aenter__()
        discovery = await client.discover()
        assert [tool.name for tool in discovery.tools] == ["echo_environment"]

    await asyncio.create_task(prepare())
    try:
        result = await asyncio.create_task(client.call_tool("echo_environment", {}))
        assert json.loads(result.content[0])["MCP_TOKEN"] == "initial-fixture-value"
    finally:
        await asyncio.create_task(client.__aexit__(None, None, None))
    assert client._client is None
    assert client._stack is None


async def test_close_before_owner_starts_finishes_the_opening_caller() -> None:
    """An immediate concurrent close must not leave startup awaiting an orphaned future."""
    client = stdio_client()
    opening = asyncio.create_task(client.__aenter__())
    await asyncio.sleep(0)
    try:
        with pytest.raises(MCPTransportError):
            await client.__aexit__(None, None, None)
        with pytest.raises(MCPTransportError):
            await asyncio.wait_for(opening, 1)
    finally:
        if not opening.done():
            opening.cancel()
            with pytest.raises(asyncio.CancelledError):
                await opening


async def test_real_sdk_reauthentication_from_another_task_replaces_transport() -> None:
    """Credential rotation closes and reopens SDK scopes without changing their owner."""
    client = stdio_client()
    await asyncio.create_task(client.__aenter__())
    credential = SecretValue("rotated-fixture-value")
    try:
        changed = await asyncio.create_task(
            client.reauthenticate(credential, build_stdio_environment(client._config, credential))
        )
        assert changed
        result = await client.call_tool("echo_environment", {})
        assert json.loads(result.content[0])["MCP_TOKEN"] == "rotated-fixture-value"
    finally:
        await asyncio.create_task(client.__aexit__(None, None, None))


async def test_real_sdk_discovery_timeout_can_close_from_cleanup_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded discovery timeout leaves teardown safe for MCPRuntime's cleanup task."""
    client = stdio_client()

    async def slow_list_tools(_client: object) -> None:
        """Pause after the actual SDK handshake has completed."""
        await asyncio.Event().wait()

    monkeypatch.setattr(ProtocolClient, "list_tools", slow_list_tools)

    async def prepare() -> None:
        """Time out the discovery phase while preserving real SDK contexts."""
        await client.__aenter__()
        async with asyncio.timeout(0.01):
            await client.discover()

    with pytest.raises(TimeoutError):
        await asyncio.create_task(prepare())
    await asyncio.create_task(client.__aexit__(None, None, None))
    assert client._owner is None
    assert client._client is None


async def test_real_sdk_startup_cancellation_unwinds_owned_contexts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during handshake cleanup cannot leak scopes into the caller task."""
    client = stdio_client()
    connected = asyncio.Event()
    closed = asyncio.Event()
    original_enter = ProtocolClient.__aenter__
    original_exit = ProtocolClient.__aexit__

    async def delayed_enter(sdk_client: ProtocolClient) -> ProtocolClient:
        """Hold real entered scopes inside the SDK's unfinished startup phase."""
        await original_enter(sdk_client)
        connected.set()
        try:
            await asyncio.Event().wait()
        finally:
            await original_exit(sdk_client, None, None, None)
            closed.set()
        return sdk_client

    monkeypatch.setattr(ProtocolClient, "__aenter__", delayed_enter)
    opening = asyncio.create_task(client.__aenter__())
    await asyncio.wait_for(connected.wait(), 10)
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(opening, 10)
    await asyncio.wait_for(closed.wait(), 10)
    with pytest.raises(MCPTransportError):
        await client.__aexit__(None, None, None)
    assert closed.is_set()
    assert client._owner is None
    assert client._stack is None
    assert client._client is None
    # Later requests remain usable after the cancelled preparation.
    monkeypatch.setattr(ProtocolClient, "__aenter__", original_enter)
    async with client:
        assert (await client.discover()).tools


async def test_real_sdk_startup_cancellation_does_not_wait_for_slow_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection timeout can reach the runtime's separately bounded cleanup wait."""
    client = stdio_client()
    connected = asyncio.Event()
    closing = asyncio.Event()
    allow_close = asyncio.Event()
    original_enter = ProtocolClient.__aenter__
    original_exit = ProtocolClient.__aexit__
    original_close = client._close_owned

    async def delayed_enter(sdk_client: ProtocolClient) -> ProtocolClient:
        """Keep startup incomplete after entering actual SDK cancellation scopes."""
        await original_enter(sdk_client)
        connected.set()
        try:
            await asyncio.Event().wait()
        finally:
            await original_exit(sdk_client, None, None, None)
        return sdk_client

    async def delayed_close(*_arguments: object) -> None:
        """Simulate slow cleanup without replacing the real transport's teardown."""
        closing.set()
        await allow_close.wait()
        await original_close(None, None, None)

    monkeypatch.setattr(ProtocolClient, "__aenter__", delayed_enter)
    monkeypatch.setattr(client, "_close_owned", delayed_close)
    opening = asyncio.create_task(client.__aenter__())
    try:
        await asyncio.wait_for(connected.wait(), 10)
        opening.cancel()
        await asyncio.wait_for(closing.wait(), 10)
        done, _pending = await asyncio.wait({opening}, timeout=0.1)
        assert opening in done, "startup cancellation waited for owner teardown"
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert client._owner is not None
        assert not client._owner.done()
    finally:
        allow_close.set()
        await asyncio.gather(opening, return_exceptions=True)
        with suppress(MCPTransportError):
            await client.__aexit__(None, None, None)
    assert client._owner is None


async def test_real_sdk_cancelled_close_caller_does_not_interrupt_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teardown finishes in its owner despite cancellation and concurrent close requests."""
    client = stdio_client()
    await asyncio.create_task(client.__aenter__())
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    original_close = client._close_owned

    async def delayed_close(*_arguments: object) -> None:
        """Keep the actual SDK transport open until the test releases teardown."""
        close_started.set()
        await allow_close.wait()
        await original_close(None, None, None)

    monkeypatch.setattr(client, "_close_owned", delayed_close)
    closing = asyncio.create_task(client.__aexit__(None, None, None))
    await asyncio.wait_for(close_started.wait(), 10)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    allow_close.set()
    await asyncio.gather(
        client.__aexit__(None, None, None),
        client.__aexit__(None, None, None),
    )
    assert client._owner is None
    assert client._stack is None
    assert client._client is None


async def test_force_close_retires_owner_behind_delayed_exit_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delayed runtime wrapper cannot keep its independently owned transport alive."""
    processes: list[Any] = []
    original_spawn = mcp_stdio._create_platform_compatible_process

    async def capture_process(*arguments: Any, **keywords: Any) -> Any:
        """Retain the actual child handle to distinguish cleanup from abandoned tasks."""
        process = await original_spawn(*arguments, **keywords)
        processes.append(process)
        return process

    monkeypatch.setattr(mcp_stdio, "_create_platform_compatible_process", capture_process)
    client = stdio_client()
    await client.__aenter__()
    owner = client._owner
    assert owner is not None
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed_exit() -> None:
        """Model a retained cleanup wrapper that has not reached the SDK close yet."""
        started.set()
        await release.wait()
        await client.__aexit__(None, None, None)

    pending = asyncio.create_task(delayed_exit())
    try:
        await started.wait()
        await asyncio.wait_for(client.force_close(), 10)
        assert owner.done()
        assert client._owner is None
        assert client._client is None
        assert client._stack is None
        assert len(processes) == 1
        assert processes[0].returncode is not None
        await client.force_close()
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        with suppress(MCPTransportError):
            await client.__aexit__(None, None, None)


async def test_force_close_unwinds_a_blocked_sdk_exit_in_its_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced cancellation must still execute the original SDK exit in the entering task."""
    started, release, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_exit = ProtocolClient.__aexit__

    async def blocked_exit(sdk_client: ProtocolClient, *arguments: Any) -> None:
        """Pause teardown while retaining its real cleanup as an unconditional finalizer."""
        started.set()
        try:
            await release.wait()
        finally:
            try:
                await original_exit(sdk_client, *arguments)
            finally:
                exited.set()

    # AsyncExitStack captures __aexit__ when entering, so replace it before connecting.
    monkeypatch.setattr(ProtocolClient, "__aexit__", blocked_exit)
    client = stdio_client()
    await client.__aenter__()
    owner = client._owner
    assert owner is not None
    pending = asyncio.create_task(client.__aexit__(None, None, None))
    try:
        await started.wait()
        await asyncio.wait_for(client.force_close(), 10)
        await asyncio.gather(pending, return_exceptions=True)
        assert exited.is_set()
        assert owner.done()
        assert client._owner is None
        assert client._client is None
        assert client._stack is None
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        with suppress(MCPTransportError):
            await client.__aexit__(None, None, None)


async def test_force_close_respects_sdk_shield_while_stdio_process_is_stopping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced closure must not interrupt the SDK's shielded subprocess termination."""
    started, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_stop = mcp_stdio._stop_server_process

    async def blocked_stop(process: Any) -> None:
        """Wait inside the actual SDK shutdown shield before stopping the real child."""
        started.set()
        await release.wait()
        await original_stop(process)
        stopped.set()

    monkeypatch.setattr(mcp_stdio, "_stop_server_process", blocked_stop)
    client = stdio_client()
    await client.__aenter__()
    owner = client._owner
    assert owner is not None
    pending = asyncio.create_task(client.__aexit__(None, None, None))
    forcing: asyncio.Task[None] | None = None
    try:
        await started.wait()
        forcing = asyncio.create_task(client.force_close())
        await asyncio.sleep(0)
        assert not owner.done()
        assert not forcing.done()
        release.set()
        await asyncio.wait_for(forcing, 10)
        await asyncio.gather(pending, return_exceptions=True)
        assert stopped.is_set()
        assert owner.done()
        assert client._owner is None
        assert client._client is None
        assert client._stack is None
    finally:
        release.set()
        if forcing is not None:
            await asyncio.gather(forcing, return_exceptions=True)
        await asyncio.gather(pending, return_exceptions=True)
        with suppress(MCPTransportError):
            await client.__aexit__(None, None, None)


async def test_force_close_before_owner_startup_is_repeatable() -> None:
    """Closing an unused client or a just-created owner never starts an orphan transport."""
    client = stdio_client()
    await client.force_close()
    await client.force_close()
    opening = asyncio.create_task(client.__aenter__())
    await asyncio.sleep(0)
    owner = client._owner
    assert owner is not None
    try:
        await asyncio.wait_for(client.force_close(), 10)
        with pytest.raises(MCPTransportError):
            await opening
        await client.force_close()
        assert owner.done()
        assert client._owner is None
        assert client._client is None
        assert client._stack is None
    finally:
        await asyncio.gather(opening, return_exceptions=True)
        with suppress(MCPTransportError):
            await client.__aexit__(None, None, None)


async def test_real_sdk_server_exit_is_a_transport_error_not_caller_cancellation(
    tmp_path: Path,
) -> None:
    """A broken stdio server must not cancel its API or worker caller."""
    server = tmp_path / "disconnecting_server.py"
    server.write_text(
        "import os\nfrom mcp.server import MCPServer\n"
        "server = MCPServer('disconnect')\n"
        "@server.tool()\ndef disconnect() -> str:\n    os._exit(1)\n"
        "server.run(transport='stdio')\n",
        encoding="utf-8",
    )
    client = stdio_client()
    client._config = client._config.model_copy(
        update={"endpoint": shlex.join([sys.executable, str(server)])}
    )
    await asyncio.create_task(client.__aenter__())
    try:
        with pytest.raises(MCPTransportError):
            await asyncio.wait_for(client.call_tool("disconnect", {}), 10)
        caller = asyncio.current_task()
        assert caller is not None
        assert caller.cancelling() == 0
        await asyncio.sleep(0)
    finally:
        await asyncio.create_task(client.__aexit__(None, None, None))
