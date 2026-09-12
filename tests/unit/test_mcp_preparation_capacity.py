"""Shared MCP startup capacity preserves HTTP progress and survives cancellation."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from agent_core.adapters.mcp.scripted import ScriptedMCPClient
from agent_core.bootstrap import build
from agent_core.domain.credentials import SecretValue
from agent_core.domain.mcp import (
    MCPDiscovery,
    MCPServerConfig,
    MCPTransport,
    ScriptedMCPServer,
)
from tests.integration.m2_support import memory_settings


async def test_queued_stdio_preserves_http_progress_and_releases_cancelled_capacity(
    tmp_path: Path,
) -> None:
    """A local startup queue leaves HTTP slots free and cancellation restores both pools."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "limits.yaml").write_text(
        "schema_version: 1\negress:\n  mode: allowlist\n  destinations:\n"
        "    - host: allowed.test\n      ports: [443]\n"
    )
    configs = [
        MCPServerConfig(
            tenant_id="local",
            server_id=f"local_{index}",
            transport=MCPTransport.STDIO,
            endpoint=f"/fixture/local_{index}",
            operator_configured=True,
        )
        for index in range(12)
    ]
    configs.append(
        MCPServerConfig(
            tenant_id="local",
            server_id="remote",
            transport=MCPTransport.HTTP,
            endpoint="https://allowed.test/mcp",
        ),
    )
    started: asyncio.Queue[MCPTransport] = asyncio.Queue()
    release = asyncio.Event()
    clients: list[ScriptedMCPClient] = []

    def factory(
        config: MCPServerConfig,
        credential: SecretValue | None,
        environment: dict[str, str],
    ) -> ScriptedMCPClient:
        """Hold local discoveries while allowing the synthetic HTTP client to finish."""

        class ControlledClient(ScriptedMCPClient):
            async def discover(self) -> MCPDiscovery:
                """Expose actual admission order without opening a socket or subprocess."""
                await started.put(config.transport)
                if config.transport is MCPTransport.STDIO:
                    await release.wait()
                return await super().discover()

        client = ControlledClient(
            ScriptedMCPServer(name=config.server_id, discovery=MCPDiscovery()),
            credential,
            environment,
        )
        clients.append(client)
        return client

    async with (
        asyncio.timeout(5),
        build(
            settings=replace(memory_settings(), config_dir=tmp_path),
            storage="memory",
            mcp_servers=tuple(configs),
            mcp_client_factory=factory,
        ) as composition,
    ):
        for cancel in (True, False):
            preparation = asyncio.create_task(
                composition.mcp.prepare(uuid4(), composition.principal)
            )
            try:
                async with asyncio.timeout(1):
                    admitted = [await started.get() for _ in range(3)]
                assert admitted.count(MCPTransport.STDIO) == 2
                assert admitted.count(MCPTransport.HTTP) == 1
                assert started.empty()
                if cancel:
                    preparation.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await preparation
                    assert all(not client.entered for client in clients)
                else:
                    release.set()
                    await preparation
            finally:
                if not preparation.done():
                    preparation.cancel()
                await asyncio.gather(preparation, return_exceptions=True)
    assert all(not client.entered for client in clients)
