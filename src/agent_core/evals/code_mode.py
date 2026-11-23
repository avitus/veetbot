"""An evaluation sandbox that runs a code-mode script for real (ADR-0342).

The fake execution environment echoes standard input, which proves nothing
about a script. This one runs the same entry point the sandbox image runs,
as a local subprocess, and serves its bridge from a socket in a temporary
directory. Every call the script makes therefore crosses the real bridge
session and the real tool pipeline. Only the deterministic harness builds it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol

from agent_core.adapters.execution.fake import FakeExecutionEnvironment
from agent_core.domain.execution import (
    BridgeEndpoint,
    EnvironmentHandle,
    ExecutionCommand,
    ExecutionResult,
    KillReason,
)

CODE_RUNNER_MODULE = "agent_core.execution.code_runner"
_MESSAGE_BYTES = 64 * 1024


class _BridgeHandler(Protocol):
    async def handle(self, request: bytes) -> bytes: ...


class ScriptExecutionEnvironment(FakeExecutionEnvironment):
    """The fake environment, except that a code-mode script actually runs."""

    async def execute_with_bridge(
        self,
        environment: EnvironmentHandle,
        command: ExecutionCommand,
        endpoint: BridgeEndpoint,
        handler: object,
    ) -> ExecutionResult:
        if tuple(command.argv[-2:]) != ("-m", CODE_RUNNER_MODULE):
            return await super().execute_with_bridge(environment, command, endpoint, handler)
        self._assert_live(environment)
        self.commands.append((environment, command))
        bridge: _BridgeHandler = handler  # type: ignore[assignment]

        async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            # What the in-sandbox relay does: add the token the script never sees.
            try:
                while request := await reader.readline():
                    payload: dict[str, Any] = json.loads(request)
                    payload["token"] = endpoint.token
                    response = await bridge.handle(
                        json.dumps(payload, separators=(",", ":")).encode("utf-8")
                    )
                    writer.write(response.rstrip(b"\n") + b"\n")
                    await writer.drain()
            finally:
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()

        with tempfile.TemporaryDirectory(prefix="code-mode-") as directory:
            socket_path = Path(directory) / "bridge.sock"
            server = await asyncio.start_unix_server(
                relay, path=str(socket_path), limit=_MESSAGE_BYTES + 1
            )
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    CODE_RUNNER_MODULE,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, "AGENT_TOOL_BRIDGE_SOCKET": str(socket_path)},
                )
                timed_out = False
                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(command.stdin or b""),
                        timeout=command.timeout_seconds,
                    )
                except TimeoutError:
                    timed_out = True
                    process.kill()
                    stdout, stderr = await process.communicate()
            finally:
                server.close()
                await server.wait_closed()
        limit = command.maximum_output_bytes
        return ExecutionResult(
            exit_code=None if timed_out else process.returncode,
            stdout=stdout[:limit],
            stderr=stderr[:limit],
            stdout_truncated=len(stdout) > limit,
            stderr_truncated=len(stderr) > limit,
            timed_out=timed_out,
            killed_by=KillReason.TIMEOUT if timed_out else None,
            files_changed=(),
            # The harness compares runs byte for byte; wall time would differ.
            duration_ms=0,
        )
