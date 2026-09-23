"""Run untrusted PDF parsing in bounded, disposable child processes."""

from __future__ import annotations

import asyncio
import json
import math
import sys
from contextlib import suppress
from typing import Literal
from weakref import WeakValueDictionary

from agent_core.domain.errors import ToolValidationError

_MAX_BYTES = 32 * 1024 * 1024
_MEMORY_BYTES = 512 * 1024 * 1024
_SLOTS: WeakValueDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = WeakValueDictionary()


async def parse_pdf(
    content: bytes, *, operation: Literal["pages", "text"], timeout: float
) -> int | str | None:
    """Kill and reap a timed-out or cancelled parser before releasing its slot."""

    if timeout <= 0 or not math.isfinite(timeout):
        raise ValueError("PDF timeout must be finite and positive")
    if len(content) > _MAX_BYTES:
        raise ToolValidationError("knowledge source exceeds the byte ceiling")
    loop = asyncio.get_running_loop()
    slots = _SLOTS.setdefault(loop, asyncio.Semaphore(2))
    # Queue waiting consumes the same deadline as parsing; no parser uses the
    # application's default thread executor.
    async with asyncio.timeout(timeout):
        async with slots:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "agent_core.adapters.pdf_process",
                operation,
                str(max(1, math.ceil(timeout))),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                output, _ = await process.communicate(content)
            finally:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.kill()
                await process.wait()
    if process.returncode != 0:
        raise ToolValidationError("knowledge source could not be read")
    try:
        result = json.loads(output)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ToolValidationError("knowledge source could not be read") from exc
    if not isinstance(result, dict):
        raise ToolValidationError("knowledge source could not be read")
    if result.get("error") == "encrypted":
        raise ToolValidationError("knowledge source is encrypted")
    value = result.get("value")
    if operation == "pages" and (value is None or type(value) is int):
        return value
    if operation == "text" and isinstance(value, str):
        return value
    raise ToolValidationError("knowledge source could not be read")


def _main() -> None:
    """The private child protocol: bounded bytes in, bounded JSON out."""

    import resource

    # The production Linux worker enforces address space; Darwin does not
    # support lowering RLIMIT_AS/RLIMIT_DATA. Its child still has the wall/CPU
    # deadlines and the same two-process concurrency ceiling.
    if sys.platform == "linux":
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        ceiling = _MEMORY_BYTES if hard == resource.RLIM_INFINITY else min(_MEMORY_BYTES, hard)
        resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))
    cpu_seconds = int(sys.argv[2])
    _, hard_cpu = resource.getrlimit(resource.RLIMIT_CPU)
    if hard_cpu != resource.RLIM_INFINITY:
        cpu_seconds = min(cpu_seconds, hard_cpu)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    content = sys.stdin.buffer.read(_MAX_BYTES + 1)
    if len(content) > _MAX_BYTES:
        raise ValueError("PDF input too large")
    try:
        if sys.argv[1] == "pages":
            from agent_core.adapters.artifacts.inspection import _pdf_pages

            value: int | str | None = _pdf_pages(content)
        elif sys.argv[1] == "text":
            from agent_core.adapters.knowledge.pdf import _pdf_text

            value = _pdf_text(content)
        else:
            raise ValueError("unknown PDF operation")
        output = json.dumps({"value": value}).encode()
    except ToolValidationError as exc:
        error = "encrypted" if "encrypted" in str(exc) else "unreadable"
        output = json.dumps({"error": error}).encode()
    if len(output) > _MAX_BYTES:
        raise ValueError("PDF output too large")
    sys.stdout.buffer.write(output)


if __name__ == "__main__":
    _main()
