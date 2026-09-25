"""A content-free Chat latency report derived from the event log (ADR-0131)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Distribution(BaseModel):
    """Percentiles of a duration in seconds, or of a count."""

    model_config = ConfigDict(frozen=True)

    count: int = Field(ge=0)
    p50: float | None = None
    p90: float | None = None
    maximum: float | None = None


class NamedDistribution(Distribution):
    name: str


class ModelTiming(BaseModel):
    """One model and reasoning effort: attempt duration, first text, tokens and cache."""

    model_config = ConfigDict(frozen=True)

    model: str
    reasoning_effort: str | None
    calls: int = Field(ge=0)
    duration: Distribution
    first_text: Distribution
    average_input_tokens: int = Field(ge=0)
    average_output_tokens: int = Field(ge=0)
    cached_input_percent: float | None


class QueueWait(BaseModel):
    """How long runs of one priority and session kind waited for a worker."""

    model_config = ConfigDict(frozen=True)

    priority: int
    session_kind: str
    wait: Distribution


class ChatLatencyReport(BaseModel):
    """Aggregates only: no message, argument, title or tool result text."""

    model_config = ConfigDict(frozen=True)

    since: datetime
    turns: int = Field(ge=0)
    phases: list[NamedDistribution] = Field(default_factory=list)
    setup: list[NamedDistribution] = Field(default_factory=list)
    models: list[ModelTiming] = Field(default_factory=list)
    tools: list[NamedDistribution] = Field(default_factory=list)
    mcp_connections: list[NamedDistribution] = Field(default_factory=list)
    mcp_pins_reused: int = Field(default=0, ge=0)
    queue: list[QueueWait] = Field(default_factory=list)
    cached_input_percent: float | None = None
    reasoning_output_percent: float | None = None


_PHASE_LABELS = (
    ("turn", "whole turn, queued to answer"),
    ("queue", "worker queue"),
    ("setup", "setup before the first model request"),
    ("model", "model calls"),
    ("tools", "tools"),
    ("model_calls", "model calls per turn (count)"),
)


def _figure(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


def _line(label: str, distribution: Distribution, width: int = 38) -> str:
    return (
        f"  {label:<{width}} {_figure(distribution.p50)} / {_figure(distribution.p90)}"
        f" / {_figure(distribution.maximum)}  ({distribution.count})"
    )


def render_latency_report(report: ChatLatencyReport) -> str:
    """Render the report as text; every figure is seconds unless it says otherwise."""

    since = report.since.strftime("%Y-%m-%d %H:%M %Z").strip()
    lines = [
        f"{report.turns} Chat turns since {since}",
        "Seconds as median / 90th percentile / maximum (count).",
    ]
    phases = {phase.name: phase for phase in report.phases}
    if phases:
        lines.append("Phases")
        lines.extend(_line(label, phases[name]) for name, label in _PHASE_LABELS if name in phases)
    if report.setup:
        lines.append("Setup before the first model request")
        lines.extend(_line(item.name, item) for item in report.setup)
    if report.models:
        lines.append("Models")
        for model in report.models:
            effort = "" if model.reasoning_effort is None else f" ({model.reasoning_effort})"
            cached = (
                "-" if model.cached_input_percent is None else f"{model.cached_input_percent:g}%"
            )
            lines.append(f"  {model.model}{effort}: {model.calls} calls")
            lines.append(_line("attempt", model.duration, width=36))
            lines.append(_line("first text", model.first_text, width=36))
            lines.append(
                f"    {model.average_input_tokens} input and {model.average_output_tokens}"
                f" output tokens on average; {cached} of input cached"
            )
    if report.tools:
        lines.append("Tools")
        lines.extend(_line(item.name, item) for item in report.tools)
    if report.mcp_connections or report.mcp_pins_reused:
        lines.append(
            f"MCP handshakes ({report.mcp_pins_reused} pins reused a remembered discovery)"
        )
        lines.extend(_line(item.name, item) for item in report.mcp_connections)
    if report.queue:
        lines.append("Queue wait by priority and session kind")
        lines.extend(
            _line(f"{item.priority} {item.session_kind}", item.wait) for item in report.queue
        )
    if report.cached_input_percent is not None:
        lines.append(f"Prompt cache served {report.cached_input_percent:g}% of input tokens.")
    if report.reasoning_output_percent is not None:
        lines.append(f"Reasoning was {report.reasoning_output_percent:g}% of output tokens.")
    return "\n".join(lines)
