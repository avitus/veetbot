"""The in-sandbox bridge relay, run as the process the sandbox runs (tool-system.md).

The relay holds the one-time turn token, so sandboxed code never needs it: the relay
stamps every forwarded request with the token, replacing anything the script wrote,
and answers malformed or oversized requests itself without forwarding them. The
Docker security suite exercises it inside a container; this runs it locally.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import stat
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

TURN_TOKEN = secrets.token_urlsafe(16)
LIMIT = 64 * 1024


@asynccontextmanager
async def _relay() -> AsyncIterator[tuple[asyncio.subprocess.Process, Path]]:
    with tempfile.TemporaryDirectory(prefix="relay-") as directory:
        socket_path = Path(directory) / ".agent" / "bridge.sock"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "agent_core.execution.bridge_relay",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "AGENT_TOOL_BRIDGE_SOCKET": str(socket_path)},
        )
        try:
            yield process, socket_path
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await process.wait()


async def _started(process: asyncio.subprocess.Process, socket_path: Path) -> None:
    assert process.stdin is not None
    process.stdin.write(TURN_TOKEN.encode() + b"\n")
    await process.stdin.drain()
    async with asyncio.timeout(10):
        while not socket_path.exists():
            assert process.returncode is None
            await asyncio.sleep(0.01)


async def test_the_relay_stamps_the_turn_token_and_answers_malformed_requests_itself() -> None:
    async with asyncio.timeout(30), _relay() as (process, socket_path):
        assert process.stdin is not None and process.stdout is not None
        await _started(process, socket_path)
        assert stat.S_IMODE(os.stat(socket_path).st_mode) == 0o600
        reader, writer = await asyncio.open_unix_connection(str(socket_path))

        writer.write(b'{"call":"a.b","arguments":{},"ordinal":0,"token":"forged"}\n')
        await writer.drain()
        forwarded = json.loads(await process.stdout.readline())
        process.stdin.write(b'{"status":"succeeded","result":{"value":1}}\n')
        await process.stdin.drain()
        answered = json.loads(await reader.readline())

        for malformed in (b"[1]\n", b"{not json\n", b"\xff\n"):
            writer.write(malformed)
        writer.write(b'{"call":"a.c","arguments":{},"ordinal":1}\n')
        await writer.drain()
        refusals = [json.loads(await reader.readline()) for _ in range(3)]
        second = json.loads(await process.stdout.readline())
        process.stdin.write(b'{"status":"denied","reason_code":"x","retryable":false}\n')
        await process.stdin.drain()
        relayed_denial = json.loads(await reader.readline())

        # A request that stamping would push past the bound is refused, not truncated.
        padding = b"x" * (LIMIT - 60)
        writer.write(b'{"call":"a.d","arguments":{"pad":"' + padding + b'"}}\n')
        await writer.drain()
        stamped_too_large = json.loads(await reader.readline())
        writer.close()

        # A line past the read bound ends that connection after one denial.
        oversized_reader, oversized_writer = await asyncio.open_unix_connection(str(socket_path))
        oversized_writer.write(b"x" * (LIMIT + 2) + b"\n")
        await oversized_writer.drain()
        too_large = json.loads(await oversized_reader.readline())
        closed = await oversized_reader.read()
        oversized_writer.close()

    assert forwarded == {"call": "a.b", "arguments": {}, "ordinal": 0, "token": TURN_TOKEN}
    assert answered == {"status": "succeeded", "result": {"value": 1}}
    assert [refusal["reason_code"] for refusal in refusals] == ["bridge.protocol_error"] * 3
    assert all(refusal["retryable"] is False for refusal in refusals)
    # None of the refused requests was forwarded: the next line is the next valid call.
    assert second == {"call": "a.c", "arguments": {}, "ordinal": 1, "token": TURN_TOKEN}
    assert relayed_denial["reason_code"] == "x"
    assert stamped_too_large["reason_code"] == "bridge.request_too_large"
    assert too_large["reason_code"] == "bridge.request_too_large"
    assert closed == b""


async def test_an_oversized_response_ends_the_relay_after_a_denial() -> None:
    async with asyncio.timeout(30), _relay() as (process, socket_path):
        assert process.stdin is not None and process.stdout is not None
        await _started(process, socket_path)
        reader, writer = await asyncio.open_unix_connection(str(socket_path))

        writer.write(b'{"call":"a.b","arguments":{},"ordinal":0}\n')
        await writer.drain()
        await process.stdout.readline()
        process.stdin.write(b"x" * (LIMIT + 2) + b"\n")
        await process.stdin.drain()
        denial = json.loads(await reader.readline())
        exit_code = await process.wait()
        writer.close()

    assert denial == {
        "status": "denied",
        "reason_code": "bridge.response_too_large",
        "retryable": False,
    }
    assert exit_code == 0


async def test_the_relay_refuses_to_start_without_a_bootstrap_token() -> None:
    async with asyncio.timeout(30), _relay() as (process, socket_path):
        assert process.stdin is not None
        process.stdin.close()
        exit_code = await process.wait()
        assert process.stderr is not None
        stderr = await process.stderr.read()

    assert exit_code != 0
    assert b"bridge bootstrap token is missing or oversized" in stderr
    assert not socket_path.exists()
