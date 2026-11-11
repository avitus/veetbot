"""Operational browser reporting preserves bounded, content-free evidence semantics."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_core.cli import main
from agent_core.observability import browser
from agent_core.observability.browser import (
    BrowserReportSample,
    aggregate_browser_report,
    render_browser_report,
)

NOW = datetime(2026, 11, 10, tzinfo=UTC)


def _diagnostics(milliseconds: int, **changes: Any) -> str:
    """Build one typed phase summary without any page or account material."""
    return json.dumps(
        {
            "version": 1,
            "elapsed_ms": milliseconds,
            "truncated": False,
            "phases": [
                {
                    "phase": "navigation",
                    "placement": "runtime",
                    "outcome": "completed",
                    "failure": "none",
                    "elapsed_ms": milliseconds,
                }
            ],
            **changes,
        }
    )


def test_report_distributions_and_evidence_gaps_do_not_conflate_missing_with_success() -> None:
    """Valid samples alone supply timings; terminal outcomes retain incomplete evidence."""
    report = aggregate_browser_report(
        [
            BrowserReportSample("browser.navigate", "completed", True, _diagnostics(1000)),
            BrowserReportSample(
                "browser.navigate", "completed", True, _diagnostics(3000, truncated=True)
            ),
            BrowserReportSample("browser.act", "uncertain", False, None),
            BrowserReportSample("browser.act", "failed", True, _diagnostics(9, version=2)),
            BrowserReportSample(
                "browser.observe", "failed", True, _diagnostics(9, url="PRIVATE_CANARY")
            ),
            BrowserReportSample("browser.upload", "failed", True, None),
            BrowserReportSample("browser.observe", "failed", True, '"' + "x" * 16384 + '"'),
        ],
        since=NOW - timedelta(hours=24),
        until=NOW,
    )
    assert report.operations == 7
    assert report.valid_diagnostics == 2
    assert report.missing_diagnostics == 1
    assert report.invalid_diagnostics == 4
    assert report.truncated_diagnostics == 1
    assert sum(item.count for item in report.outcomes) == 7
    [duration] = report.operation_timings
    assert (
        duration.count,
        duration.p50_seconds,
        duration.p95_seconds,
        duration.maximum_seconds,
    ) == (2, 2, 2.9, 3)
    [phase] = report.phases
    assert phase.count == 2 and phase.p95_seconds == 2.9
    assert "PRIVATE_CANARY" not in report.model_dump_json() + render_browser_report(report)
    assert "do not sum" in render_browser_report(report)


def test_phase_overlap_is_not_added_to_operation_wall_time() -> None:
    """An outer phase and an inner phase can each cover the same elapsed interval."""
    phases = [
        {"phase": phase, "placement": "runtime", "outcome": "completed", "elapsed_ms": 1000}
        for phase in ("navigation", "readiness")
    ]
    report = aggregate_browser_report(
        [
            BrowserReportSample(
                "browser.navigate", "completed", True, _diagnostics(1000, phases=phases)
            )
        ],
        since=NOW - timedelta(hours=1),
        until=NOW,
    )
    assert report.operation_timings[0].maximum_seconds == 1
    assert len(report.phases) == 2
    assert all(phase.maximum_seconds == 1 for phase in report.phases)


def test_report_stops_at_its_bound_and_marks_selection_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extra sentinel detects truncation without aggregating or consuming later samples."""
    monkeypatch.setattr(browser, "MAX_BROWSER_REPORT_OPERATIONS", 2)
    consumed: list[int] = []

    def samples() -> Any:
        """Fail if aggregation consumes beyond its single truncation sentinel."""
        for index in range(4):
            consumed.append(index)
            assert index < 3
            yield BrowserReportSample("browser.act", "failed", False, None)

    report = aggregate_browser_report(samples(), since=NOW - timedelta(hours=1), until=NOW)
    assert report.operations == 2 and report.selection_truncated
    assert consumed == [0, 1, 2]


@pytest.mark.parametrize("json_output", [False, True])
def test_browser_report_cli_is_reserved_read_only_and_bounded(
    monkeypatch: pytest.MonkeyPatch, json_output: bool
) -> None:
    """The command selects reporting rather than interpreting the name as a run prompt."""
    seen: list[int] = []

    async def report(hours: int) -> browser.BrowserOperationReport:
        """Return empty evidence and record the requested time window."""
        seen.append(hours)
        return aggregate_browser_report([], since=NOW - timedelta(hours=hours), until=NOW)

    monkeypatch.setattr(main, "_browser_report", report)
    arguments = ["run", "browser-report", "--hours", "48"] + (["--json"] if json_output else [])
    result = CliRunner().invoke(main.app, arguments)
    assert result.exit_code == 0, result.output
    assert seen == [48]
    assert "browser-report" in main.RUN_RESERVED_WORDS
    if json_output:
        assert json.loads(result.output)["operations"] == 0
    else:
        assert "no success claim" in result.output


@pytest.mark.parametrize("hours", ["0", "2161"])
def test_browser_report_cli_rejects_out_of_bounds_windows(hours: str) -> None:
    """Invalid windows fail at the command boundary before composition or database access."""
    result = CliRunner().invoke(main.app, ["run", "browser-report", "--hours", hours])
    assert result.exit_code == 2


def test_browser_report_dependency_failure_does_not_print_private_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dependency failure remains unsuccessful without exposing its exception message."""

    async def unavailable(_hours: int) -> Any:
        """Model a private database failure without opening a connection."""
        raise RuntimeError("PRIVATE_DATABASE_URL")

    monkeypatch.setattr(main, "_browser_report", unavailable)
    result = CliRunner().invoke(main.app, ["run", "browser-report"])
    assert result.exit_code == 1
    assert "unavailable" in result.output
    assert "PRIVATE_DATABASE_URL" not in result.output


async def test_report_composition_uses_one_clock_and_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI forwards a single bounded interval into its configured principal's report."""
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    seen: list[tuple[datetime, datetime]] = []

    async def report(since: datetime, until: datetime) -> browser.BrowserOperationReport:
        """Capture the window while returning an empty, well-formed population."""
        seen.append((since, until))
        return aggregate_browser_report([], since=since, until=until)

    @asynccontextmanager
    async def build(**options: Any) -> AsyncIterator[SimpleNamespace]:
        """Replace the database composition without exposing a run submission surface."""
        assert options == {"storage": "postgres"}
        yield SimpleNamespace(browser_report=report, clock=SimpleNamespace(now=lambda: NOW))

    monkeypatch.setattr(main, "build", build)
    assert await main._browser_report(24) is not None
    assert seen == [(NOW - timedelta(hours=24), NOW)]


@pytest.mark.parametrize(
    "encoded",
    ["null", "[]", "{}", "not json", _diagnostics(1, phases=[{"phase": "PRIVATE_PHASE_CANARY"}])],
)
def test_invalid_diagnostic_objects_never_contribute_partial_evidence(encoded: str) -> None:
    """Reject the entire invalid summary without copying an untrusted phase into output."""
    report = aggregate_browser_report(
        [BrowserReportSample("browser.observe", "failed", True, encoded)],
        since=NOW - timedelta(hours=1),
        until=NOW,
    )
    assert report.invalid_diagnostics == 1
    assert report.phases == [] and report.operation_timings == []
    assert "PRIVATE_PHASE_CANARY" not in report.model_dump_json() + render_browser_report(report)


def test_failure_groups_keep_the_closed_phase_and_reason() -> None:
    """A timeout is counted by its typed phase and never translated into a page-derived label."""
    encoded = _diagnostics(
        500,
        phases=[
            {
                "phase": "navigation",
                "placement": "hosted",
                "outcome": "failed",
                "failure": "timeout",
                "elapsed_ms": 500,
            }
        ],
    )
    report = aggregate_browser_report(
        [BrowserReportSample("browser.navigate", "failed", True, encoded)],
        since=NOW - timedelta(hours=1),
        until=NOW,
    )
    [phase] = report.phases
    assert (phase.phase, phase.placement, phase.outcome, phase.failure, phase.count) == (
        "navigation",
        "hosted",
        "failed",
        "timeout",
        1,
    )
