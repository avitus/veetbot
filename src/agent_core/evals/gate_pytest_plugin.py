"""Private pytest reporter used by ``agent eval gates`` subprocesses."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

_REPORTS: dict[str, list[str]] = {}
logger = logging.getLogger(__name__)


def pytest_runtest_logreport(report: Any) -> None:
    if report.failed:
        outcome = "failed"
    elif report.skipped:
        outcome = "skipped"
    elif report.when == "call" and report.passed:
        outcome = "passed"
    else:
        return
    _REPORTS.setdefault(str(report.nodeid), []).append(outcome)


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    del session
    destination = os.environ.get("AGENT_GATE_REPORT")
    if destination is None:
        return
    path = Path(destination)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"exitstatus": exitstatus, "reports": _REPORTS}, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        logger.exception("could not write gate report to %s", path)
