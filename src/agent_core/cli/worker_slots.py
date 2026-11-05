"""Run one worker process per slot of a role, under one systemd unit (ADR-0295).

Each slot is a whole process, because the executor shares state across one
process's runs and has only ever run one at a time in each. Separate processes
already share the queue safely through its leases.
"""

import asyncio
import contextlib
import signal
from collections.abc import Sequence


async def supervise_worker_slots(
    commands: Sequence[Sequence[str]], *, grace_seconds: float = 55
) -> int:
    """Run every command until a signal or the first exit, then stop them all.

    A slot that exits on its own takes the others down with a failure status, so
    the unit's restart policy brings back the whole set. A worker finishes its
    current run before it stops, so a slot still running after ``grace_seconds``
    is killed and its run comes back when its lease expires. The default ends
    before the unit's ``TimeoutStopSec``.
    """
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopping.set)
    children: list[asyncio.subprocess.Process] = []
    try:
        for command in commands:
            children.append(await asyncio.create_subprocess_exec(*command))
        exits = [asyncio.create_task(child.wait()) for child in children]
        stop = asyncio.create_task(stopping.wait())
        finished, _ = await asyncio.wait([*exits, stop], return_when=asyncio.FIRST_COMPLETED)
        stop.cancel()
        signalled = stopping.is_set()
        first_status = next((task.result() for task in exits if task in finished), 0)
        for child in children:
            with contextlib.suppress(ProcessLookupError):
                child.terminate()
        await asyncio.wait(exits, timeout=grace_seconds)
        return 0 if signalled else first_status or 1
    finally:
        for child in children:
            if child.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    child.kill()
                await child.wait()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)
