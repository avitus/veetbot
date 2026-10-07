"""Browser evidence fails closed without copying test or page diagnostics."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from tests import browser_tasks, real_browser_support
from tests.browser_report import BrowserReporter


@pytest.fixture(autouse=True)
def isolated_task_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_tasks, "OUTCOMES", {})


@pytest.mark.parametrize("outcome", ["passed", "failed", "skipped", "not_run", "empty"])
def test_browser_baseline_requires_every_case_and_an_observed_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    monkeypatch.setattr(
        real_browser_support, "OBSERVED_BROWSER_BUILDS", {"123.4.5.6"}, raising=False
    )
    monkeypatch.setattr(
        browser_tasks,
        "OUTCOMES",
        {
            scenario.id: browser_tasks.TaskOutcome(True, 2, scenario.expected_effects, 0.1)
            for scenario in browser_tasks.SCENARIOS
        },
    )
    destination = tmp_path / "baseline.json"
    reporter = BrowserReporter(destination)
    canary = "CREDENTIAL_CANARY"
    nodeid = f"tests/unit/test_fixture.py::test_login[{canary}]"
    session = cast(
        pytest.Session,
        SimpleNamespace(
            items=[] if outcome == "empty" else [SimpleNamespace(nodeid=nodeid)], exitstatus=0
        ),
    )
    reporter.pytest_collection_finish(session)
    if outcome not in {"not_run", "empty"}:
        reporter.pytest_runtest_logreport(
            cast(
                pytest.TestReport,
                SimpleNamespace(
                    nodeid=nodeid,
                    when="call",
                    duration=0.25,
                    failed=outcome == "failed",
                    skipped=outcome == "skipped",
                    longrepr=canary,
                    sections=[("captured stdout", canary)],
                ),
            )
        )
    reporter.pytest_sessionfinish(session, 0)
    raw = destination.read_text()
    report = json.loads(raw)
    assert canary not in raw
    assert report["verified"] is (outcome == "passed")
    assert (session.exitstatus == 0) is (outcome == "passed")
    assert report["browser_builds"] == ["123.4.5.6"]
    assert isinstance(report["source_dirty"], bool)
    assert report["live_task_quality_measured"] is False
    if outcome != "empty":
        assert report["cases"][0]["duration_seconds"] == (0 if outcome == "not_run" else 0.25)


def test_browser_baseline_cannot_verify_without_launching_chromium(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(real_browser_support, "OBSERVED_BROWSER_BUILDS", set(), raising=False)
    reporter = BrowserReporter(tmp_path / "baseline.json")
    reporter.cases["case"] = {"id": "case", "outcome": "passed", "duration_seconds": 0}
    session = cast(pytest.Session, SimpleNamespace(exitstatus=0))
    reporter.pytest_sessionfinish(session, 0)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    assert json.loads(reporter.destination.read_text())["verified"] is False


def test_filtered_browser_cases_cannot_produce_a_complete_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(real_browser_support, "OBSERVED_BROWSER_BUILDS", {"123.4.5.6"})
    reporter = BrowserReporter(tmp_path / "baseline.json")
    reporter.cases["case"] = {"id": "case", "outcome": "passed", "duration_seconds": 0}
    reporter.pytest_deselected(
        [
            cast(pytest.Item, SimpleNamespace(get_closest_marker=lambda name: object())),
            cast(pytest.Item, SimpleNamespace(get_closest_marker=lambda name: None)),
        ]
    )
    session = cast(pytest.Session, SimpleNamespace(exitstatus=0))
    reporter.pytest_sessionfinish(session, 0)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    report = json.loads(reporter.destination.read_text())
    assert report["verified"] is False
    assert report["deselected_browser_cases"] == 1


def test_browser_baseline_declares_scripted_task_evidence_separately(tmp_path: Path) -> None:
    reporter = BrowserReporter(tmp_path / "baseline.json")
    session = cast(pytest.Session, SimpleNamespace(exitstatus=0))
    reporter.pytest_sessionfinish(session, 0)
    report = json.loads(reporter.destination.read_text())
    assert report["synthetic_tasks"]["kind"] == "scripted_browser_component_tasks"
    assert report["synthetic_tasks"]["model_calls"] == 0
    assert report["synthetic_tasks"]["live_task_quality_measured"] is False
    assert report["synthetic_tasks"]["verified"] is False


def test_missing_synthetic_task_fails_an_otherwise_passing_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(real_browser_support, "OBSERVED_BROWSER_BUILDS", {"123.4.5.6"})
    for scenario in browser_tasks.SCENARIOS[:-1]:
        browser_tasks.OUTCOMES[scenario.id] = browser_tasks.TaskOutcome(
            True, 2, scenario.expected_effects, 0.1
        )
    reporter = BrowserReporter(tmp_path / "baseline.json")
    reporter.cases["case"] = {"id": "case", "outcome": "passed", "duration_seconds": 0}
    session = cast(pytest.Session, SimpleNamespace(exitstatus=0))
    reporter.pytest_sessionfinish(session, 0)
    report = json.loads(reporter.destination.read_text())
    assert not report["verified"] and session.exitstatus == pytest.ExitCode.TESTS_FAILED
    assert report["synthetic_tasks"]["cases"][-1]["outcome"] == "not_run"
