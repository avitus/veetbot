"""Real stdio SDK connections keep AnyIO context ownership within one task."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from contextlib import suppress
from pathlib import Path

import pytest
from mcp import Client as ProtocolClient

from agent_core.adapters.mcp.sdk import SDKMCPClient
from agent_core.domain.credentials import SecretValue
from agent_core.domain.errors import MCPTransportError
from agent_core.domain.mcp import MCPAuthScheme, MCPServerConfig, MCPTransport
from agent_core.mcp.configuration import build_stdio_environment


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
