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
import signal
import sys
import tempfile
from contextlib import suppress
from dataclasses import dataclass
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
_REAP_SECONDS = 5


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

        relays: set[asyncio.Task[None]] = set()

        async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            # What the in-sandbox relay does: add the token the script never sees.
            current = asyncio.current_task()
            if current is not None:
                relays.add(current)
            try:
                while request := await reader.readline():
                    payload: dict[str, Any] = json.loads(request)
                    payload["token"] = endpoint.token
                    # As UTF-8, the size the script's library measured.
                    response = await bridge.handle(
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                            "utf-8"
                        )
                    )
                    writer.write(response.rstrip(b"\n") + b"\n")
                    await writer.drain()
            finally:
                if current is not None:
                    relays.discard(current)
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()

        with tempfile.TemporaryDirectory(prefix="code-mode-") as directory:
            socket_path = Path(directory) / "bridge.sock"
            server = await asyncio.start_unix_server(
                relay, path=str(socket_path), limit=_MESSAGE_BYTES + 1
            )
            try:
                captured = await _run_bounded(
                    command, {**os.environ, "AGENT_TOOL_BRIDGE_SOCKET": str(socket_path)}
                )
            finally:
                server.close()
                # A script that was ended may have left a call waiting in the
                # bridge, on an approval for example. The server waits for
                # every open connection, so that call is abandoned first.
                held = tuple(relays)
                for task in held:
                    task.cancel()
                await asyncio.gather(*held, return_exceptions=True)
                await server.wait_closed()
        limit = command.maximum_output_bytes
        ended_early = captured.timed_out or captured.over_limit
        return ExecutionResult(
            exit_code=None if ended_early else captured.exit_code,
            stdout=captured.stdout[:limit],
            stderr=captured.stderr[:limit],
            stdout_truncated=len(captured.stdout) > limit,
            stderr_truncated=len(captured.stderr) > limit,
            timed_out=captured.timed_out,
            killed_by=(
                KillReason.TIMEOUT
                if captured.timed_out
                else KillReason.OUTPUT_LIMIT
                if captured.over_limit
                else None
            ),
            files_changed=(),
            # The harness compares runs byte for byte; wall time would differ.
            duration_ms=0,
        )


@dataclass(slots=True)
class _Captured:
    stdout: bytes = b""
    stderr: bytes = b""
    exit_code: int | None = None
    timed_out: bool = False
    over_limit: bool = False


async def _run_bounded(command: ExecutionCommand, environment: dict[str, str]) -> _Captured:
    """Run the script in its own process group and hold it to the command's bounds.

    Output is read as it is written and never kept past one byte over the
    ceiling, so a script cannot fill the evaluator's memory. A timeout, the
    ceiling or a cancelled evaluation ends the whole group, so nothing the
    script started keeps a pipe open or runs on afterwards.
    """

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        CODE_RUNNER_MODULE,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
        start_new_session=True,
    )
    script_input, script_output, script_errors = process.stdin, process.stdout, process.stderr
    assert script_input is not None and script_output is not None and script_errors is not None
    captured = _Captured()
    limit = command.maximum_output_bytes
    over_limit = asyncio.Event()

    async def feed(stdin: asyncio.StreamWriter) -> None:
        with suppress(ConnectionError):
            stdin.write(command.stdin or b"")
            await stdin.drain()
        stdin.close()
        with suppress(ConnectionError):
            await stdin.wait_closed()

    async def capture(stream: asyncio.StreamReader, name: str) -> None:
        kept = bytearray()
        while chunk := await stream.read(65536):
            kept += chunk[: limit + 1 - len(kept)]
            setattr(captured, name, bytes(kept))
            if len(kept) > limit:
                over_limit.set()
                return

    async def finish() -> None:
        await asyncio.gather(
            feed(script_input),
            capture(script_output, "stdout"),
            capture(script_errors, "stderr"),
        )
        if not over_limit.is_set():
            await process.wait()

    work = asyncio.create_task(finish())
    limit_reached = asyncio.create_task(over_limit.wait())
    try:
        done, _pending = await asyncio.wait(
            {work, limit_reached},
            timeout=command.timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        captured.over_limit = over_limit.is_set()
        captured.timed_out = not done
        if work in done and not captured.over_limit:
            work.result()
            captured.exit_code = process.returncode
        return captured
    finally:
        # Runs on every path, cancellation included: end the group, stop the
        # readers, and wait a bounded time for the script to be reaped.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        for task in (work, limit_reached):
            task.cancel()
        await asyncio.gather(work, limit_reached, return_exceptions=True)
        with suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(process.wait()), timeout=_REAP_SECONDS)
