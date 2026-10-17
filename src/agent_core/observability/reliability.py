"""Content-free reliability measures and explicit operational alarm thresholds."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class FailureGroup(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str
    error_class: str
    fingerprint: str | None = None
    release_id: str | None = None
    count: int = Field(ge=0)


class ReliabilityReport(BaseModel):
    """Terminal outcomes in the window, plus current ready-queue health."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    since: datetime
    until: datetime
    completed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    cancelled: int = Field(default=0, ge=0)
    internal_errors: int = Field(default=0, ge=0)
    completion_rate: float | None = Field(default=None, ge=0, le=1)
    p95_completion_seconds: float | None = Field(default=None, ge=0)
    queued: int = Field(default=0, ge=0)
    running: int = Field(default=0, ge=0)
    waiting: int = Field(default=0, ge=0)
    oldest_ready_queue_seconds: float = Field(default=0, ge=0)
    stale_leases: int = Field(default=0, ge=0)
    tool_failures: int = Field(default=0, ge=0)
    nonfatal_tool_failures: int = Field(default=0, ge=0)
    model_failures: int = Field(default=0, ge=0)
    nonfatal_model_failures: int = Field(default=0, ge=0)
    failures: list[FailureGroup] = Field(default_factory=list)
    failure_groups_truncated: bool = False


class ReliabilityThresholds(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    minimum_outcomes: int = Field(default=10, ge=1)
    failure_rate: float = Field(default=0.2, gt=0, le=1)
    internal_errors: int = Field(default=3, ge=1)
    queue_seconds: float = Field(default=120, gt=0)


def degraded_signals(
    report: ReliabilityReport, thresholds: ReliabilityThresholds
) -> frozenset[str]:
    """No traffic cannot satisfy an availability claim or trigger an error-rate alarm."""

    signals: set[str] = set()
    outcomes = report.completed + report.failed
    if (
        outcomes >= thresholds.minimum_outcomes
        and report.failed / outcomes >= thresholds.failure_rate
    ):
        signals.add("run_failure_rate")
    if report.internal_errors >= thresholds.internal_errors:
        signals.add("internal_errors")
    if report.oldest_ready_queue_seconds >= thresholds.queue_seconds:
        signals.add("queue_lag")
    if report.stale_leases:
        signals.add("stale_leases")
    return frozenset(signals)


def render_reliability_report(report: ReliabilityReport) -> str:
    """Format aggregate health and safe failure groups without private event content."""
    rate = (
        "No completed or failed runs in this window"
        if report.completion_rate is None
        else f"{report.completion_rate:.1%} completion rate"
    )
    lines = [
        f"Reliability from {report.since.isoformat()} to {report.until.isoformat()}",
        f"{rate}; {report.completed} completed, {report.failed} failed, "
        f"{report.cancelled} cancelled.",
        f"Current state: {report.queued} queued, {report.running} running, "
        f"{report.waiting} waiting for input or approval.",
        f"Oldest ready queue wait: {report.oldest_ready_queue_seconds:g}s; "
        f"stale leases: {report.stale_leases}.",
        f"Tool failures: {report.tool_failures} "
        f"({report.nonfatal_tool_failures} in completed runs).",
        f"Model failures: {report.model_failures} "
        f"({report.nonfatal_model_failures} in completed runs).",
        "Failure groups (no message, prompt, email or tool-result content):",
    ]
    lines.extend(
        f"  {item.count}: {item.reason} / {item.error_class}; "
        f"release={item.release_id or 'unknown'}; fingerprint={item.fingerprint or 'unavailable'}"
        for item in report.failures
    )
    if report.failure_groups_truncated:
        lines.append("Only the fifty most frequent failure groups are shown.")
    return "\n".join(lines)
