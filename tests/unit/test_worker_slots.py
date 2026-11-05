"""The async role runs one process per configured slot (ADR-0295)."""

import asyncio
import inspect
import os
import re
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_core.cli import main
from agent_core.cli.main import WorkerRole
from agent_core.cli.worker_slots import supervise_worker_slots
from tests.integration.m2_support import memory_settings

RUN_UNTIL_TERMINATED = """
import pathlib, signal, sys, time
def stop(*_):
    pathlib.Path(sys.argv[1]).write_text("stopped")
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
pathlib.Path(sys.argv[1]).write_text("started")
while True:
    time.sleep(0.01)
"""


def _sleeper(path: Path) -> list[str]:
    """Build a child that marks its path when it can take SIGTERM, and when it has."""
    return [sys.executable, "-c", RUN_UNTIL_TERMINATED, str(path)]


async def _until(path: Path, content: str) -> None:
    """Wait for a child's marker, failing the test if it never appears."""
    async with asyncio.timeout(10):
        while not path.exists() or path.read_text() != content:
            await asyncio.sleep(0.01)


async def test_a_signal_stops_every_slot_and_exits_cleanly(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    supervision = asyncio.create_task(supervise_worker_slots([_sleeper(first), _sleeper(second)]))
    await _until(first, "started")
    await _until(second, "started")
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(supervision, 10) == 0
    assert first.read_text() == "stopped"
    assert second.read_text() == "stopped"


@pytest.mark.parametrize(("child_exit", "expected"), [(7, 7), (0, 1)])
async def test_a_slot_that_exits_stops_the_rest_so_the_unit_restarts(
    tmp_path: Path, child_exit: int, expected: int
) -> None:
    survivor = tmp_path / "survivor"
    exits = [sys.executable, "-c", f"import sys; sys.exit({child_exit})"]
    status = await asyncio.wait_for(supervise_worker_slots([exits, _sleeper(survivor)]), 10)
    assert status == expected, "systemd restarts this unit only on a failure status"
    assert not survivor.exists() or survivor.read_text() == "stopped"


IGNORES_TERMINATION = """
import os, pathlib, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))
while True:
    time.sleep(0.01)
"""

EXITS_ONCE_ANOTHER_RUNS = """
import pathlib, sys, time
while not pathlib.Path(sys.argv[1]).exists():
    time.sleep(0.01)
time.sleep(0.05)
sys.exit(7)
"""


def _assert_gone(marker: Path) -> None:
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


async def test_a_stalled_slot_cannot_stop_the_unit_restarting(tmp_path: Path) -> None:
    stalled = tmp_path / "stalled"
    commands = [
        [sys.executable, "-c", EXITS_ONCE_ANOTHER_RUNS, str(stalled)],
        [sys.executable, "-c", IGNORES_TERMINATION, str(stalled)],
    ]
    status = await asyncio.wait_for(supervise_worker_slots(commands, grace_seconds=0.2), 5)
    assert status == 7
    _assert_gone(stalled)


async def test_a_stalled_slot_cannot_outlast_a_requested_stop(tmp_path: Path) -> None:
    stalled = tmp_path / "stalled"
    commands = [[sys.executable, "-c", IGNORES_TERMINATION, str(stalled)]]
    supervision = asyncio.create_task(supervise_worker_slots(commands, grace_seconds=0.2))
    async with asyncio.timeout(10):
        while not stalled.exists() or not stalled.read_text():
            await asyncio.sleep(0.01)
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(supervision, 5) == 0
    _assert_gone(stalled)


def test_the_grace_ends_before_systemd_would_kill_the_unit() -> None:
    unit = Path("deploy/systemd/veetbot-async-worker.service").read_text(encoding="utf-8")
    stop_timeout = int(re.search(r"^TimeoutStopSec=(\d+)s$", unit, re.M).group(1))  # type: ignore[union-attr]
    grace = inspect.signature(supervise_worker_slots).parameters["grace_seconds"].default
    assert grace < stop_timeout


def _record_cli(
    monkeypatch: pytest.MonkeyPatch, slots: int
) -> tuple[list[WorkerRole], list[list[str]]]:
    served: list[WorkerRole] = []
    supervised: list[list[str]] = []

    async def serve(role: WorkerRole) -> None:
        served.append(role)

    async def supervise(commands: Sequence[Sequence[str]]) -> int:
        supervised.extend(list(command) for command in commands)
        return 0

    monkeypatch.setattr(main, "_serve_worker", serve)
    monkeypatch.setattr(main, "supervise_worker_slots", supervise)
    monkeypatch.setattr(main, "_async_worker_slots", lambda: slots)
    return served, supervised


def test_the_async_role_starts_one_single_slot_process_per_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served, supervised = _record_cli(monkeypatch, slots=2)
    monkeypatch.setattr(sys, "argv", ["/opt/veetbot/current/.venv/bin/agent"])
    result = CliRunner().invoke(main.app, ["worker", "--role", "async"])
    assert result.exit_code == 0
    assert served == []
    single = ["/opt/veetbot/current/.venv/bin/agent", "worker", "--role", "async", "--single-slot"]
    assert supervised == [single, single]


def test_a_single_slot_process_serves_the_role_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    served, supervised = _record_cli(monkeypatch, slots=2)
    result = CliRunner().invoke(main.app, ["worker", "--role", "async", "--single-slot"])
    assert result.exit_code == 0
    assert served == [WorkerRole.ASYNC]
    assert supervised == []


def test_one_configured_slot_needs_no_supervisor(monkeypatch: pytest.MonkeyPatch) -> None:
    served, supervised = _record_cli(monkeypatch, slots=1)
    assert CliRunner().invoke(main.app, ["worker", "--role", "async"]).exit_code == 0
    assert served == [WorkerRole.ASYNC]
    assert supervised == []


def test_other_roles_stay_one_process(monkeypatch: pytest.MonkeyPatch) -> None:
    served, supervised = _record_cli(monkeypatch, slots=2)
    assert CliRunner().invoke(main.app, ["worker", "--role", "interactive"]).exit_code == 0
    assert served == [WorkerRole.INTERACTIVE]
    assert supervised == []


def test_production_runs_two_async_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "load_settings", memory_settings)
    assert main._async_worker_slots() == 2
