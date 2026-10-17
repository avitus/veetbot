"""Operator reliability reads are bounded, read-only by default and machine-readable."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_core.cli import main


def test_reliability_report_uses_a_bounded_window_and_does_not_notify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that reliability report uses a bounded window and does not notify."""
    calls: list[tuple[int, bool]] = []

    async def report(hours: int, notify: bool, _thresholds: object) -> object:
        """Record the requested bounded window and return a read-only fixture report."""
        calls.append((hours, notify))
        return SimpleNamespace(model_dump_json=lambda **_: '{"completed":7,"failed":2}')

    monkeypatch.setattr(main, "operator_reliability", report)
    result = CliRunner().invoke(main.app, ["run", "reliability", "--hours", "24", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"completed": 7, "failed": 2}
    assert calls == [(24, False)]


@pytest.mark.parametrize("hours", ["0", "2161"])
def test_reliability_window_rejects_unbounded_reads(hours: str) -> None:
    """Verify that reliability window rejects unbounded reads."""
    result = CliRunner().invoke(main.app, ["run", "reliability", "--hours", hours])
    assert result.exit_code == 2


def test_reliability_dependency_failure_is_unsuccessful_and_content_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that reliability dependency failure is unsuccessful and content free."""

    async def unavailable(*_args: Any) -> object:
        """Raise a private synthetic dependency failure without any external request."""
        raise RuntimeError("PRIVATE_DATABASE_CONNECTION")

    monkeypatch.setattr(main, "_reliability_report", unavailable)
    result = CliRunner().invoke(main.app, ["run", "reliability", "--check"])
    assert result.exit_code == 1
    assert "unavailable" in result.output
    assert "PRIVATE_DATABASE_CONNECTION" not in result.output


def test_reliability_notify_requires_check() -> None:
    """Verify that reliability notify requires check."""
    result = CliRunner().invoke(main.app, ["run", "reliability", "--notify"])
    assert result.exit_code == 2
    assert "--notify requires --check" in result.output


@pytest.mark.parametrize("notify", [False, True])
def test_reliability_check_reports_degradation_and_only_notifies_explicitly(
    monkeypatch: pytest.MonkeyPatch, notify: bool
) -> None:
    """Verify that reliability check reports degradation and only notifies explicitly."""
    from agent_core.observability.reliability import ReliabilityReport

    now = datetime(2026, 10, 14, tzinfo=UTC)
    calls: list[bool] = []

    async def report(_hours: int, should_notify: bool, _thresholds: object) -> ReliabilityReport:
        """Record notification opt-in and return synthetic degraded health."""
        calls.append(should_notify)
        return ReliabilityReport(since=now - timedelta(hours=1), until=now, internal_errors=3)

    monkeypatch.setattr(main, "_reliability_report", report)
    arguments = ["run", "reliability", "--check", "--json"]
    if notify:
        arguments.append("--notify")
    result = CliRunner().invoke(main.app, arguments)
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["internal_errors"] == 3
    assert calls == [notify]
