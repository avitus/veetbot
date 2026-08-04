"""Authored, no-socket MCP transport for deterministic evaluation."""

from __future__ import annotations

import hmac
from collections import deque
from types import TracebackType
from typing import Self

from agent_core.domain.credentials import SecretValue
from agent_core.domain.errors import MCPTransportError, MCPUnauthorizedError
from agent_core.domain.mcp import (
    MCPCallResult,
    MCPDiscovery,
    MCPServerConfig,
    ScriptedMCPServer,
)


class ScriptedMCPClient:
    def __init__(
        self,
        script: ScriptedMCPServer,
        credential: SecretValue | None,
        environment: dict[str, str],
    ) -> None:
        self._script = script
        self._credential = credential
        self.environment = dict(environment)
        self._responses = deque(script.responses)
        self.entered = False
        self.reauthentication_count = 0
        self.call_count = 0

    async def __aenter__(self) -> Self:
        if self._script.connect_outcome == "disconnect":
            raise MCPTransportError
        if self._script.connect_outcome == "unauthorized":
            raise MCPUnauthorizedError
        self.entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.entered = False

    async def discover(self) -> MCPDiscovery:
        if not self.entered:
            raise MCPTransportError
        return self._script.discovery.model_copy(deep=True)

    def _next(self, operation: str, name: str | None) -> MCPCallResult:
        if not self.entered or not self._responses:
            raise MCPTransportError
        response = self._responses.popleft()
        if response.operation != operation or response.name != name:
            raise AssertionError("scripted MCP request did not match the authored response")
        self.call_count += 1
        if response.outcome == "disconnect":
            raise MCPTransportError
        if response.outcome == "unauthorized":
            raise MCPUnauthorizedError
        return response.result.model_copy(deep=True)

    async def call_tool(self, name: str, arguments: dict[str, object]) -> MCPCallResult:
        del arguments
        return self._next("call_tool", name)

    async def read_resource(self, uri: str | None) -> MCPCallResult:
        return self._next("read_resource", uri)

    async def reauthenticate(
        self,
        credential: SecretValue | None,
        environment: dict[str, str],
    ) -> bool:
        self.reauthentication_count += 1
        previous = None if self._credential is None else self._credential.reveal().encode()
        replacement = None if credential is None else credential.reveal().encode()
        changed = (
            previous != replacement
            if previous is None or replacement is None
            else not hmac.compare_digest(previous, replacement)
        )
        if changed:
            self._credential = credential
            self.environment = dict(environment)
        return changed


class ScriptedMCPClientFactory:
    def __init__(self, scripts: dict[str, ScriptedMCPServer]) -> None:
        self._scripts = dict(scripts)
        self.created: list[ScriptedMCPClient] = []

    def __call__(
        self,
        config: MCPServerConfig,
        credential: SecretValue | None,
        environment: dict[str, str],
    ) -> ScriptedMCPClient:
        try:
            script = self._scripts[config.server_id]
        except KeyError as exc:
            raise MCPTransportError from exc
        client = ScriptedMCPClient(script, credential, environment)
        self.created.append(client)
        return client
