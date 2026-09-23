"""PDF timeout and cancellation stop parsing before the next attempt."""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agent_core.adapters.artifacts import inspection
from agent_core.adapters.knowledge import pdf
from agent_core.domain.errors import ToolValidationError


@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize("startup_delay", [0, 0.1])
@pytest.mark.parametrize("operation", ["inspect", "extract"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_pdf_worker_stops_on_timeout_or_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    cancel: bool,
    count: int,
    startup_delay: float,
) -> None:
    finished = tmp_path / "parse-finished"

    def slow_parse(content: bytes) -> Any:
        time.sleep(0.3)
        finished.write_text("work escaped its lifetime")
        return 1 if operation == "inspect" else "text"

    monkeypatch.setattr(inspection, "_pdf_pages", slow_parse)
    monkeypatch.setattr(pdf, "_pdf_text", slow_parse)
    create = asyncio.create_subprocess_exec
    processes: list[asyncio.subprocess.Process] = []
    worker_tasks: set[asyncio.Task[Any]] = set()
    started = asyncio.Event()
    peak_workers = 0

    async def slow_worker(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        nonlocal peak_workers
        task = asyncio.current_task()
        assert task is not None
        worker_tasks.add(task)
        # A worker owns its slot during subprocess startup as well as parsing.
        peak_workers = max(peak_workers, sum(not item.done() for item in worker_tasks))
        await asyncio.sleep(startup_delay)
        script = (
            "import sys,time; from pathlib import Path; sys.stdin.buffer.read(); "
            f"time.sleep({10 if cancel else 0.3}); "
            f"Path({str(finished)!r}).write_text('work escaped its lifetime')"
        )
        process = await create(sys.executable, "-c", script, **kwargs)
        processes.append(process)
        if len(processes) == min(count, 2):
            started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_worker)

    async def source() -> AsyncIterator[bytes]:
        yield b"%PDF-1.4"

    async def parse() -> None:
        timeout = 10 if cancel else 0.05
        if operation == "inspect":
            facts = await inspection.SignatureAttachmentInspector(
                pdf_timeout_seconds=timeout
            ).inspect(b"%PDF-1.4", filename="a.pdf", declared_media_type="application/pdf")
            assert facts.kind == "other"
        else:
            with pytest.raises(ToolValidationError, match="could not be read"):
                await pdf.PdfTextExtractor(maximum_bytes=1024, timeout_seconds=timeout).extract(
                    source(), "application/pdf"
                )

    tasks = [asyncio.create_task(parse()) for _ in range(count)]
    if cancel:
        # Exercise cancellation of actual children, independently of spawn speed.
        await asyncio.wait_for(started.wait(), timeout=10)
        for task in tasks:
            task.cancel()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
        assert len(processes) == min(count, 2)
    else:
        await asyncio.gather(*tasks)
    await asyncio.sleep(0.4)
    assert not finished.exists(), "PDF work continued after timeout/cancellation"
    assert 1 <= peak_workers <= 2
    assert all(process.returncode is not None for process in processes)
    monkeypatch.undo()
    from tests.contract.test_attachment_inspector_contract import _pdf

    facts = await inspection.SignatureAttachmentInspector().inspect(
        _pdf(1), filename="recovered.pdf", declared_media_type="application/pdf"
    )
    assert facts.page_count == 1
