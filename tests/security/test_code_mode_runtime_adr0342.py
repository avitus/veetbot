"""Code mode against a real container runtime, never the fake (ADR-0342).

The offline suites run a script as a local subprocess. These run the real
`sandbox.run_code` tool over the Docker adapter: the image's own runner and
library, the in-sandbox relay, the scrubbed environment and the network
denial. They also time a first and a later script, which is the local half
of the latency evidence the ADR asks for.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from agent_core.adapters.determinism import RandomIdFactory, SystemClock
from agent_core.adapters.execution.docker import (
    DockerExecutionEnvironment,
    resolve_local_image_digest,
)
from agent_core.domain.execution import ResourceLimits
from agent_core.domain.tools import ToolExecutionContext, ToolResult
from agent_core.execution.manager import SandboxManager
from agent_core.tools.sandbox_run_code import SandboxRunCodeTool
from tests.contract.support import tool_context

pytestmark = [pytest.mark.integration, pytest.mark.sandbox]

_PAGES = {
    ("demo.renewals", 1): {"items": ["Acme", "Birch", "Cobalt"], "next_page": 2},
    ("demo.renewals", 2): {"items": ["Dune", "Globex"], "next_page": None},
    ("demo.overdue", 1): {"items": ["Globex", "Harbor", "Acme"], "next_page": 2},
    ("demo.overdue", 2): {"items": ["Ivory"], "next_page": None},
}
_TWO_LISTS = """
def everything(listing):
    names, page = set(), 1
    while page is not None:
        reply = listing(page=page)
        names.update(reply["items"])
        page = reply["next_page"]
    return names

both = everything(tools.demo.renewals) & everything(tools.demo.overdue)
print(", ".join(sorted(both)))
"""


class _Bridge:
    """The worker's side of the bridge: scripted pages and one refusal."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
        del call_id
        self.calls.append((name, arguments))
        if name == "demo.send":
            return {"status": "denied", "reason_code": "policy.denied", "retryable": False}
        page = _PAGES[(name, arguments["page"])]
        return {
            "status": "succeeded",
            "result": [{"kind": "text", "text": json.dumps(page)}],
            "retryable": False,
        }


@pytest.fixture
async def sandbox() -> AsyncIterator[SandboxManager]:
    adapter = DockerExecutionEnvironment(
        SystemClock(), RandomIdFactory(), hard_cap_seconds=60, reaper_grace_seconds=0
    )
    manager = SandboxManager(
        adapter,
        image_digest=await resolve_local_image_digest("agent-core-sandbox:dev"),
        limits=ResourceLimits(
            cpu_millicores=500,
            memory_bytes=256 * 1024 * 1024,
            pids_max=64,
            workspace_bytes=2 * 1024 * 1024,
            inodes_max=1000,
            wall_clock_seconds=60,
        ),
    )
    try:
        yield manager
    finally:
        await manager.close()


def _context(bridge: _Bridge, *, run: int, call_id: str = "call-code") -> ToolExecutionContext:
    return replace(
        tool_context(),
        run_id=UUID(int=run),
        call_id=call_id,
        bridge_dispatch=bridge,
        timeout_seconds=30,
        maximum_output_bytes=64 * 1024,
    )


async def _run(
    sandbox: SandboxManager, bridge: _Bridge, source: str, *, run: int, call_id: str = "call-code"
) -> tuple[ToolResult, dict[str, Any]]:
    result = await SandboxRunCodeTool(sandbox).execute(
        {"code": source}, _context(bridge, run=run, call_id=call_id)
    )
    assert result.ok, result.failure
    assert result.structured is not None
    return result, result.structured


async def test_a_script_pages_two_lists_through_the_real_relay(sandbox: SandboxManager) -> None:
    bridge = _Bridge()

    _, output = await _run(sandbox, bridge, _TWO_LISTS, run=3421)

    assert (output["exit_code"], output["stderr"]) == (0, "")
    assert output["stdout"] == "Acme, Globex\n"
    assert output["tool_calls"] == 4
    assert bridge.calls == [
        ("demo.renewals", {"page": 1}),
        ("demo.renewals", {"page": 2}),
        ("demo.overdue", {"page": 1}),
        ("demo.overdue", {"page": 2}),
    ]


async def test_a_script_runs_a_command_and_keeps_its_files_for_the_next_script(
    sandbox: SandboxManager,
) -> None:
    """Decision 10: with the command tool out of Chat's roster, a script runs one."""
    bridge = _Bridge()
    first = """
import pathlib, subprocess
done = subprocess.run(["python", "-c", "print(6 * 7)"], capture_output=True, text=True, check=True)
pathlib.Path("answer.txt").write_text(done.stdout)
print(subprocess.run(["dot", "-V"], capture_output=True, text=True).returncode)
"""

    _, wrote = await _run(sandbox, bridge, first, run=3422, call_id="call-1")
    _, read = await _run(
        sandbox, bridge, "print(open('answer.txt').read().strip())", run=3422, call_id="call-2"
    )

    assert (wrote["exit_code"], wrote["stdout"]) == (0, "0\n")
    assert [change["path"] for change in wrote["files_changed"]] == ["answer.txt"]
    assert (read["exit_code"], read["stdout"]) == (0, "42\n")


async def test_a_script_sees_no_token_no_network_and_a_refusal_it_can_read(
    sandbox: SandboxManager,
) -> None:
    bridge = _Bridge()
    source = """
import os, socket
print(sorted(name for name in os.environ if "TOKEN" in name or "KEY" in name))
try:
    socket.create_connection(("1.1.1.1", 443), timeout=3)
    print("connected")
except OSError:
    print("no network")
try:
    tools.demo.send(to="someone")
except ToolCallError as refusal:
    print(refusal.status, refusal.reason_code)
"""

    _, output = await _run(sandbox, bridge, source, run=3423)

    assert output["exit_code"] == 0, output["stderr"]
    assert output["stdout"] == "[]\nno network\ndenied policy.denied\n"


async def test_an_uncaught_error_ends_the_script_with_its_traceback(
    sandbox: SandboxManager,
) -> None:
    _, output = await _run(sandbox, _Bridge(), "print('before')\n1 / 0\n", run=3424)

    assert output["exit_code"] != 0
    assert output["stdout"] == "before\n"
    assert "ZeroDivisionError" in output["stderr"]


async def test_the_first_and_a_later_script_are_timed(sandbox: SandboxManager) -> None:
    """Local evidence only; the production sandbox is measured before activation."""
    bridge = _Bridge()
    timings: list[tuple[str, float, int]] = []
    for label, call_id in (("first", "call-1"), ("second", "call-2"), ("third", "call-3")):
        started = time.perf_counter()
        _, output = await _run(sandbox, bridge, _TWO_LISTS, run=3425, call_id=call_id)
        timings.append((label, time.perf_counter() - started, output["duration_ms"]))
        assert output["stdout"] == "Acme, Globex\n"
    started = time.perf_counter()
    _, plain = await _run(sandbox, bridge, "print(6 * 7)", run=3425, call_id="call-4")
    timings.append(("no tool calls", time.perf_counter() - started, plain["duration_ms"]))

    for label, wall, inside in timings:
        print(f"CODE_MODE_TIMING {label}: {wall:.2f} s wall, {inside} ms in the sandbox")
    # A later script reuses the run's container, so it cannot cost a new one.
    assert timings[1][1] < timings[0][1] + 5
