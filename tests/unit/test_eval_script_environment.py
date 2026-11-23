"""The evaluation sandbox that runs a code-mode script for real (ADR-0342).

It runs a script as a local subprocess, so it has to hold that subprocess to
the same bounds a sandbox would: an output ceiling applied while the script
runs, a timeout that ends everything the script started, and no process left
behind when the evaluation is cancelled.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

import pytest

from agent_core.adapters.determinism import RandomIdFactory, SystemClock
from agent_core.domain.execution import (
    BridgeEndpoint,
    EgressPolicy,
    EnvironmentHandle,
    EnvironmentSpec,
    ExecutionCommand,
    ExecutionResult,
    KillReason,
    ResourceLimits,
)
from agent_core.evals.code_mode import CODE_RUNNER_MODULE, ScriptExecutionEnvironment


class _Echo:
    """A bridge that answers every request with the length of each argument."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def handle(self, request: bytes) -> bytes:
        payload = json.loads(request)
        self.requests.append({**payload, "bytes": len(request)})
        return json.dumps(
            {
                "status": "succeeded",
                "result": [
                    {
                        "kind": "text",
                        "text": json.dumps(
                            {name: len(value) for name, value in payload["arguments"].items()}
                        ),
                    }
                ],
                "retryable": False,
            }
        ).encode()


async def _environment() -> tuple[ScriptExecutionEnvironment, EnvironmentHandle]:
    environment = ScriptExecutionEnvironment(SystemClock(), RandomIdFactory())
    handle = await environment.provision(
        EnvironmentSpec(
            tenant_id="tenant",
            run_id=UUID(int=0x5C),
            lease_epoch=0,
            image_digest="sha256:" + "0" * 64,
            limits=ResourceLimits(
                cpu_millicores=500,
                memory_bytes=64 * 1024 * 1024,
                pids_max=32,
                workspace_bytes=1024 * 1024,
                inodes_max=100,
                wall_clock_seconds=60,
            ),
            egress=EgressPolicy(),
            environment={},
        )
    )
    return environment, handle


async def _run(
    source: str, *, timeout: int = 20, limit: int = 4096, bridge: _Echo | None = None
) -> ExecutionResult:
    environment, handle = await _environment()
    return await environment.execute_with_bridge(
        handle,
        ExecutionCommand(
            argv=("python", "-m", CODE_RUNNER_MODULE),
            working_directory=PurePosixPath("."),
            timeout_seconds=timeout,
            stdin=source.encode(),
            maximum_output_bytes=limit,
        ),
        BridgeEndpoint(socket_path=PurePosixPath("/unused"), token="turn-token"),  # noqa: S106
        bridge or _Echo(),
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_a_request_outside_ascii_reaches_the_bridge_at_its_own_size() -> None:
    bridge = _Echo()

    result = await _run("print(tools.demo.echo(text='é' * 11_000)['text'])", bridge=bridge)

    assert (result.exit_code, result.stdout) == (0, b"11000\n")
    # 22,000 bytes of text, not 66,000 bytes of escapes.
    assert bridge.requests[0]["bytes"] < 30_000
    assert bridge.requests[0]["token"] == "turn-token"  # noqa: S105 - a test value


async def test_output_past_the_ceiling_ends_the_script_instead_of_filling_memory() -> None:
    started = time.monotonic()

    result = await _run(
        "import sys\nwhile True:\n    sys.stdout.write('x' * 65536)\n", timeout=30, limit=4096
    )

    assert time.monotonic() - started < 15
    assert (len(result.stdout), result.stdout_truncated) == (4096, True)
    assert result.killed_by is KillReason.OUTPUT_LIMIT
    assert result.timed_out is False


async def test_output_within_the_ceiling_is_returned_whole() -> None:
    result = await _run("import sys\nprint('out')\nprint('err', file=sys.stderr)", limit=4096)

    assert (result.exit_code, result.stdout, result.stderr) == (0, b"out\n", b"err\n")
    assert (result.stdout_truncated, result.stderr_truncated, result.killed_by) == (
        False,
        False,
        None,
    )


async def test_a_timeout_ends_what_the_script_started_and_returns_promptly(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "child.pid"
    source = f"""
import subprocess, time
child = subprocess.Popen(["sleep", "600"])
open({str(pid_file)!r}, "w").write(str(child.pid))
print("started", flush=True)
time.sleep(600)
"""
    started = time.monotonic()

    result = await _run(source, timeout=2)

    assert time.monotonic() - started < 15
    assert (result.timed_out, result.killed_by, result.exit_code) == (
        True,
        KillReason.TIMEOUT,
        None,
    )
    assert result.stdout == b"started\n"
    child = int(pid_file.read_text())
    async with asyncio.timeout(5):
        while _alive(child):
            await asyncio.sleep(0.05)


class _Held(_Echo):
    """A bridge whose call never returns, as one waiting on an approval does not."""

    def __init__(self) -> None:
        super().__init__()
        self.cancelled = False

    async def handle(self, request: bytes) -> bytes:
        self.requests.append(json.loads(request))
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return b""


async def test_a_timeout_does_not_wait_for_a_bridge_call_that_is_still_held() -> None:
    bridge = _Held()

    # Without a bound of its own the old shutdown waited on the held call forever.
    result = await asyncio.wait_for(
        _run("tools.demo.slow()\nprint('never')", timeout=2, bridge=bridge), timeout=20
    )

    assert (result.timed_out, result.killed_by, result.stdout) == (True, KillReason.TIMEOUT, b"")
    assert len(bridge.requests) == 1
    # The held call is abandoned with the script that made it.
    assert bridge.cancelled is True


async def test_cancelling_the_evaluation_ends_the_script(tmp_path: Path) -> None:
    pid_file = tmp_path / "script.pid"
    source = f"""
import os, time
open({str(pid_file)!r}, "w").write(str(os.getpid()))
time.sleep(600)
"""
    running = asyncio.create_task(_run(source, timeout=120))
    async with asyncio.timeout(20):
        while not pid_file.exists() or not pid_file.read_text():
            await asyncio.sleep(0.05)
    script = int(pid_file.read_text())

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    async with asyncio.timeout(5):
        while _alive(script):
            await asyncio.sleep(0.05)
