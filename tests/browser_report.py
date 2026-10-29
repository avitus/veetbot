"""Content-free runtime baseline; this never reports live task quality."""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest

from tests import browser_tasks, real_browser_support


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--browser-report", help="Write required browser baseline JSON")


def pytest_configure(config: pytest.Config) -> None:
    destination = config.getoption("browser_report")
    if destination:
        real_browser_support.OBSERVED_BROWSER_BUILDS.clear()
        browser_tasks.OUTCOMES.clear()
        # An xdist worker only ships what it observed; the controller reports.
        if hasattr(config, "workerinput"):
            config.pluginmanager.register(WorkerEvidence(), "browser-evidence")
        else:
            config.pluginmanager.register(BrowserReporter(Path(destination)), "browser-baseline")


EVIDENCE_KEY = "browser_evidence"


class WorkerEvidence:
    """Hand one xdist worker's builds, task outcomes and filtering to the controller."""

    def __init__(self) -> None:
        self.deselected_browser_cases = 0

    def pytest_deselected(self, items: list[pytest.Item]) -> None:
        self.deselected_browser_cases += sum(
            item.get_closest_marker("browser") is not None for item in items
        )

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        session.config.workeroutput[EVIDENCE_KEY] = {  # type: ignore[attr-defined]
            "browser_builds": sorted(real_browser_support.OBSERVED_BROWSER_BUILDS),
            "task_outcomes": {
                scenario: asdict(outcome) for scenario, outcome in browser_tasks.OUTCOMES.items()
            },
            "deselected_browser_cases": self.deselected_browser_cases,
        }


class BrowserReporter:
    """Collect only closed outcomes, case identities and elapsed time."""

    def __init__(self, destination: Path) -> None:
        self.destination = destination
        self.cases: dict[str, dict[str, Any]] = {}
        self.deselected_browser_cases = 0

    def pytest_deselected(self, items: list[pytest.Item]) -> None:
        self.deselected_browser_cases += sum(
            item.get_closest_marker("browser") is not None for item in items
        )

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self._register([item.nodeid for item in session.items])

    def _register(self, nodeids: list[str]) -> None:
        ordinals: Counter[str] = Counter()
        for nodeid in nodeids:
            # Parameter IDs can contain values from fixtures. Retain the test
            # function and an ordinal, never arbitrary parameter representations.
            identity = nodeid.partition("[")[0]
            ordinals[identity] += 1
            self.cases[nodeid] = {
                "id": f"{identity}#{ordinals[identity]}",
                "outcome": "not_run",
                "duration_seconds": 0.0,
            }

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node: Any, ids: list[str]) -> None:
        # Every worker collects the same cases; the first list defines them.
        if not self.cases:
            self._register(ids)

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: Any, error: object) -> None:
        evidence = getattr(node, "workeroutput", {}).get(EVIDENCE_KEY)
        if evidence is None:
            return
        real_browser_support.OBSERVED_BROWSER_BUILDS.update(evidence["browser_builds"])
        for scenario, outcome in evidence["task_outcomes"].items():
            browser_tasks.OUTCOMES[scenario] = browser_tasks.TaskOutcome(**outcome)
        # Each worker filters the same collection, so counts are not summed.
        self.deselected_browser_cases = max(
            self.deselected_browser_cases, evidence["deselected_browser_cases"]
        )

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        case = self.cases.get(report.nodeid)
        if case is None:
            return
        case["duration_seconds"] += report.duration
        if report.failed:
            case["outcome"] = "failed"
        elif report.skipped and case["outcome"] != "failed":
            case["outcome"] = "skipped"
        elif report.when == "call" and case["outcome"] == "not_run":
            case["outcome"] = "passed"

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        counts = {
            outcome: sum(case["outcome"] == outcome for case in self.cases.values())
            for outcome in ("passed", "failed", "skipped", "not_run")
        }
        builds = sorted(real_browser_support.OBSERVED_BROWSER_BUILDS)
        synthetic = browser_tasks.task_report()
        verified = (
            synthetic["verified"]
            and bool(self.cases)
            and bool(builds)
            and self.deselected_browser_cases == 0
            and counts["passed"] == len(self.cases)
            and exitstatus == 0
        )
        if not verified and exitstatus == 0:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        report = {
            "schema_version": 2,
            "synthetic_tasks": synthetic,
            "kind": "browser_runtime_regression",
            "live_task_quality_measured": False,
            "source_revision": _revision(),
            "source_dirty": _dirty(),
            "playwright_version": version("playwright"),
            "browser_builds": builds,
            "verified": verified,
            "deselected_browser_cases": self.deselected_browser_cases,
            "counts": counts,
            "cases": sorted(self.cases.values(), key=lambda case: case["id"]),
        }
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        self.destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def _revision() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and len(value) == 40 else None


def _dirty() -> bool | None:
    result = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, check=False
    )
    return bool(result.stdout.strip()) if result.returncode == 0 else None
