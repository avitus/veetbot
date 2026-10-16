"""Operational checks distinguish missing evidence from failure and recovery."""

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_core.observability.reliability import (
    ReliabilityReport,
    ReliabilityThresholds,
    degraded_signals,
)


def test_only_observed_outcomes_contribute_to_error_rate() -> None:
    now = datetime(2026, 10, 14, tzinfo=UTC)
    report = ReliabilityReport(since=now - timedelta(hours=1), until=now, cancelled=100, waiting=20)
    assert degraded_signals(report, ReliabilityThresholds()) == frozenset()
    assert report.completion_rate is None
    report = report.model_copy(update={"completed": 8, "failed": 2})
    assert degraded_signals(report, ReliabilityThresholds()) == {"run_failure_rate"}


def test_queue_and_internal_error_alarms_do_not_require_high_traffic() -> None:
    now = datetime(2026, 10, 14, tzinfo=UTC)
    report = ReliabilityReport(
        since=now - timedelta(hours=1),
        until=now,
        internal_errors=3,
        oldest_ready_queue_seconds=121,
        stale_leases=1,
    )
    assert degraded_signals(report, ReliabilityThresholds()) == {
        "internal_errors",
        "queue_lag",
        "stale_leases",
    }


def test_monitor_script_is_inert_without_explicit_enablement(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[2] / "deploy/ops/reliability-check.sh"
    result = subprocess.run(
        ["sh", str(script)],
        cwd=tmp_path,
        env={**os.environ, "AGENT_RELIABILITY_MONITOR_ENABLED": "0"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0 and not result.stdout and not result.stderr
