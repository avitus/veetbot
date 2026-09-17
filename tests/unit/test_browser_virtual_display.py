"""Private virtual display lifecycle for headed authentication ceremonies."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from agent_core.adapters.browser.virtual_display import XvfbDisplay, platform_virtual_display


def _display_server(tmp_path: Path, body: str) -> str:
    """Write a stand-in display server that receives Xvfb's real command line."""
    script = tmp_path / "server.py"
    script.write_text(
        "import os, sys, time\n"
        "arguments = sys.argv[1:]\n"
        "descriptor = int(arguments[arguments.index('-displayfd') + 1])\n"
        f"pid_file = {str(tmp_path / 'pid')!r}\n"
        "open(pid_file, 'w').write(str(os.getpid()))\n" + body
    )
    launcher = tmp_path / "Xvfb"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    launcher.chmod(0o700)
    return str(launcher)


def _process_exists(tmp_path: Path) -> bool:
    try:
        os.kill(int((tmp_path / "pid").read_text()), 0)
    except ProcessLookupError:
        return False
    return True


async def test_display_reports_the_server_chosen_name_and_dies_on_close(tmp_path: Path) -> None:
    executable = _display_server(tmp_path, "os.write(descriptor, b'42\\n')\ntime.sleep(60)\n")
    display = XvfbDisplay(executable=executable)

    assert await display.start() == ":42"
    assert _process_exists(tmp_path)

    await display.close()

    assert not _process_exists(tmp_path)


async def test_display_refuses_tcp_clients(tmp_path: Path) -> None:
    executable = _display_server(
        tmp_path,
        f"open({str(tmp_path / 'arguments')!r}, 'w').write(' '.join(arguments))\n"
        "os.write(descriptor, b'7\\n')\ntime.sleep(60)\n",
    )
    display = XvfbDisplay(executable=executable)

    await display.start()
    await display.close()

    assert "-nolisten tcp" in (tmp_path / "arguments").read_text()


async def test_display_that_exits_before_reporting_a_name_fails_the_start(tmp_path: Path) -> None:
    display = XvfbDisplay(executable=_display_server(tmp_path, "sys.exit(1)\n"))

    with pytest.raises(OSError, match="virtual display did not start"):
        await display.start()


async def test_display_that_never_reports_a_name_is_killed(tmp_path: Path) -> None:
    display = XvfbDisplay(
        executable=_display_server(tmp_path, "time.sleep(60)\n"),
        start_timeout_seconds=1.0,
    )

    with pytest.raises(OSError, match="virtual display did not start"):
        await display.start()

    assert not _process_exists(tmp_path)


async def test_cancelled_start_kills_the_server_it_launched(tmp_path: Path) -> None:
    """Cancellation is not a timeout, and nobody else holds the process yet."""
    display = XvfbDisplay(executable=_display_server(tmp_path, "time.sleep(60)\n"))
    pid_file = tmp_path / "pid"
    start = asyncio.create_task(display.start())
    async with asyncio.timeout(10):
        while not (pid_file.exists() and pid_file.read_text()):
            await asyncio.sleep(0.01)

    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start

    assert not _process_exists(tmp_path)


@pytest.mark.parametrize(("platform", "virtual"), [("linux", True), ("darwin", False)])
def test_only_linux_needs_a_virtual_display(
    monkeypatch: pytest.MonkeyPatch, platform: str, virtual: bool
) -> None:
    monkeypatch.setattr(sys, "platform", platform)

    assert isinstance(platform_virtual_display(), XvfbDisplay) is virtual


async def test_close_lets_the_server_remove_its_own_lock_and_socket(tmp_path: Path) -> None:
    executable = _display_server(
        tmp_path,
        "import signal\n"
        f"marker = {str(tmp_path / 'cleaned')!r}\n"
        "def clean(*_):\n"
        "    open(marker, 'w').write('cleaned')\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, clean)\n"
        "os.write(descriptor, b'5\\n')\ntime.sleep(60)\n",
    )
    display = XvfbDisplay(executable=executable)

    await display.start()
    await display.close()

    assert (tmp_path / "cleaned").exists()


async def test_close_kills_a_server_that_ignores_termination(tmp_path: Path) -> None:
    executable = _display_server(
        tmp_path,
        "import signal\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "os.write(descriptor, b'5\\n')\ntime.sleep(60)\n",
    )
    display = XvfbDisplay(executable=executable, stop_timeout_seconds=0.2)

    await display.start()
    await display.close()

    assert not _process_exists(tmp_path)
