"""Private virtual display for one headed authentication ceremony."""

from __future__ import annotations

import asyncio
import os
import sys
from asyncio.subprocess import DEVNULL, Process
from contextlib import suppress

SCREEN_GEOMETRY = "1280x720x24"


class XvfbDisplay:
    """Own one Xvfb server that a single ceremony's browser draws to."""

    def __init__(
        self,
        *,
        executable: str = "Xvfb",
        start_timeout_seconds: float = 10.0,
        stop_timeout_seconds: float = 2.0,
    ) -> None:
        """Remember how to start the display server; nothing runs until start."""
        self._executable = executable
        self._start_timeout_seconds = start_timeout_seconds
        self._stop_timeout_seconds = stop_timeout_seconds
        self._process: Process | None = None

    async def start(self) -> str:
        """Start the server and return the display name it chose for itself."""
        # The server picks a free display number and reports it on this pipe
        # once it accepts clients, so concurrent ceremonies cannot collide.
        read_descriptor, write_descriptor = os.pipe()
        try:
            self._process = await asyncio.create_subprocess_exec(
                self._executable,
                "-displayfd",
                str(write_descriptor),
                "-screen",
                "0",
                SCREEN_GEOMETRY,
                "-nolisten",
                "tcp",
                pass_fds=(write_descriptor,),
                stdin=DEVNULL,
                stdout=DEVNULL,
                stderr=DEVNULL,
                env={"PATH": os.defpath},
            )
        except BaseException:
            os.close(read_descriptor)
            raise
        finally:
            os.close(write_descriptor)
        reader = asyncio.StreamReader()
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader),
            os.fdopen(read_descriptor, "rb", buffering=0),
        )
        try:
            reported = await asyncio.wait_for(reader.readline(), self._start_timeout_seconds)
        except TimeoutError:
            reported = b""
        finally:
            transport.close()
        number = reported.strip().decode("ascii", "replace")
        if not number.isdigit():
            await self.close()
            raise OSError("virtual display did not start")
        return f":{number}"

    async def close(self) -> None:
        """Let the server remove its lock and socket, and kill one that will not exit."""
        process, self._process = self._process, None
        if process is None:
            return
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), self._stop_timeout_seconds)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()


def platform_virtual_display() -> XvfbDisplay | None:
    """Linux servers have no display for a headed browser; other platforms do."""
    return XvfbDisplay() if sys.platform.startswith("linux") else None
