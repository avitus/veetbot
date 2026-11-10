"""Bounded, content-free browser operation aggregates from validated diagnostics."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from agent_core.domain.browser_diagnostics import (
    BrowserDiagnostics,
    Failure,
    Outcome,
    Phase,
    Placement,
)

MAX_BROWSER_REPORT_OPERATIONS = 10_000
MAX_BROWSER_DIAGNOSTIC_BYTES = 16_384
BrowserTool = Literal["browser.navigate", "browser.observe", "browser.act", "browser.upload"]
TerminalOutcome = Literal["completed", "failed", "uncertain"]


@dataclass(frozen=True)
class BrowserReportSample:
    """The only event columns admitted by the report's persistence projection."""

    tool: BrowserTool
    outcome: TerminalOutcome
    diagnostics_present: bool
    diagnostics: str | None


class DurationDistribution(BaseModel):
    """Elapsed seconds; percentiles use linear interpolation between sorted observations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    count: int = Field(ge=1)
    p50_seconds: float = Field(ge=0, le=3600)
    p95_seconds: float = Field(ge=0, le=3600)
    maximum_seconds: float = Field(ge=0, le=3600)


class BrowserOutcomeCount(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: BrowserTool
    outcome: TerminalOutcome
    count: int = Field(ge=1)


class BrowserOperationTiming(DurationDistribution):
    tool: BrowserTool
    outcome: TerminalOutcome


class BrowserPhaseTiming(DurationDistribution):
    phase: Phase
    placement: Placement
    outcome: Outcome
    failure: Failure


class BrowserOperationReport(BaseModel):
    """An explicit evidence population, never page content or a task-success score."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    since: AwareDatetime
    until: AwareDatetime
    operations: int = Field(ge=0, le=MAX_BROWSER_REPORT_OPERATIONS)
    valid_diagnostics: int = Field(ge=0, le=MAX_BROWSER_REPORT_OPERATIONS)
    missing_diagnostics: int = Field(ge=0, le=MAX_BROWSER_REPORT_OPERATIONS)
    invalid_diagnostics: int = Field(ge=0, le=MAX_BROWSER_REPORT_OPERATIONS)
    truncated_diagnostics: int = Field(ge=0, le=MAX_BROWSER_REPORT_OPERATIONS)
    selection_truncated: bool
    outcomes: list[BrowserOutcomeCount] = Field(default_factory=list)
    operation_timings: list[BrowserOperationTiming] = Field(default_factory=list)
    phases: list[BrowserPhaseTiming] = Field(default_factory=list)


def _distribution(values: list[int]) -> DurationDistribution:
    """Measure each elapsed observation once without summing overlapping phases."""
    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        """Interpolate in milliseconds, then convert the result to seconds."""
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        value = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
        return round(value / 1000, 6)

    return DurationDistribution(
        count=len(ordered),
        p50_seconds=percentile(0.5),
        p95_seconds=percentile(0.95),
        maximum_seconds=ordered[-1] / 1000,
    )


def aggregate_browser_report(
    samples: Iterable[BrowserReportSample], *, since: datetime, until: datetime
) -> BrowserOperationReport:
    """Validate entire bounded summaries and count evidence gaps without exposing their text."""
    outcomes: Counter[tuple[BrowserTool, TerminalOutcome]] = Counter()
    timings: dict[tuple[BrowserTool, TerminalOutcome], list[int]] = defaultdict(list)
    phases: dict[tuple[Phase, Placement, Outcome, Failure], list[int]] = defaultdict(list)
    operations = valid = missing = invalid = truncated = 0
    selection_truncated = False
    for sample in samples:
        if operations == MAX_BROWSER_REPORT_OPERATIONS:
            selection_truncated = True
            break
        operations += 1
        key = sample.tool, sample.outcome
        outcomes[key] += 1
        if not sample.diagnostics_present:
            missing += 1
            continue
        encoded = sample.diagnostics
        if encoded is None or len(encoded.encode()) > MAX_BROWSER_DIAGNOSTIC_BYTES:
            invalid += 1
            continue
        try:
            diagnostics = BrowserDiagnostics.model_validate_json(encoded)
        except (ValidationError, ValueError):
            invalid += 1
            continue
        valid += 1
        truncated += diagnostics.truncated
        timings[key].append(diagnostics.elapsed_ms)
        for phase in diagnostics.phases:
            phases[phase.phase, phase.placement, phase.outcome, phase.failure].append(
                phase.elapsed_ms
            )
    return BrowserOperationReport(
        since=since,
        until=until,
        operations=operations,
        valid_diagnostics=valid,
        missing_diagnostics=missing,
        invalid_diagnostics=invalid,
        truncated_diagnostics=truncated,
        selection_truncated=selection_truncated,
        outcomes=[
            BrowserOutcomeCount(tool=tool, outcome=outcome, count=count)
            for (tool, outcome), count in sorted(outcomes.items())
        ],
        operation_timings=[
            BrowserOperationTiming(tool=tool, outcome=outcome, **_distribution(values).model_dump())
            for (tool, outcome), values in sorted(timings.items())
        ],
        phases=[
            BrowserPhaseTiming(
                phase=phase,
                placement=placement,
                outcome=outcome,
                failure=failure,
                **_distribution(values).model_dump(),
            )
            for (phase, placement, outcome, failure), values in sorted(phases.items())
        ],
    )


def render_browser_report(report: BrowserOperationReport) -> str:
    """Render fixed labels, evidence coverage and duration distributions only."""
    lines = [
        f"Browser operations from {report.since.isoformat()} to {report.until.isoformat()}",
        f"{report.operations} terminal operations; {report.valid_diagnostics} valid diagnostics, "
        f"{report.missing_diagnostics} missing, {report.invalid_diagnostics} invalid, "
        f"{report.truncated_diagnostics} truncated.",
        "Policy refusals before execution and unfinished operations are excluded.",
    ]
    if report.selection_truncated:
        lines.append(
            f"Selection truncated to the newest {MAX_BROWSER_REPORT_OPERATIONS} operations."
        )
    if not report.operations:
        lines.append(
            "No terminal browser operations in this window; no success claim is available."
        )
    lines.extend(f"  {item.tool} / {item.outcome}: {item.count}" for item in report.outcomes)
    lines.append("Seconds: median / 95th percentile / maximum (count).")
    for item in report.operation_timings:
        lines.append(
            f"  {item.tool} / {item.outcome}: {item.p50_seconds:g} / {item.p95_seconds:g}"
            f" / {item.maximum_seconds:g} ({item.count})"
        )
    lines.append("Phases overlap; do not sum their durations into operation wall time.")
    for phase in report.phases:
        lines.append(
            f"  {phase.phase} / {phase.placement} / {phase.outcome} / {phase.failure}: "
            f"{phase.p50_seconds:g} / {phase.p95_seconds:g} / "
            f"{phase.maximum_seconds:g} ({phase.count})"
        )
    return "\n".join(lines)
