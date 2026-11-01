"""Recall health: content-free aggregates over traces, usage events and the store.

The report answers the operator's question "is recall delivering what the
store holds?" with counts, shares and latencies only: no statement text, no
query text, no identifiers. Citation figures are diagnostics; the primary
signals are what was delivered, what was dropped, what decayed, and whether
the semantic index covers the live store.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ClassShare(BaseModel):
    """How many items of one class fell into a bucket, with the bucket's total."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    total: int = Field(default=0, ge=0)
    flagged: int = Field(default=0, ge=0)
    provisional: int = Field(default=0, ge=0)
    source_learned: int = Field(default=0, ge=0)
    owner_authority: int = Field(default=0, ge=0)


class LifecycleCount(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    event_type: str
    authority: str
    count: int = Field(ge=0)
    minimum_confidence: float | None = Field(default=None, ge=0, le=1)


class MemoryHealthReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    since: datetime
    until: datetime
    # Usage feedback (diagnostic).
    recalled_runs: int = Field(default=0, ge=0)
    # Runs whose automatic recall was refused or timed out at some step (ADR-0281).
    limited_runs: int = Field(default=0, ge=0)
    limited_exhausted: int = Field(default=0, ge=0)
    limited_timed_out: int = Field(default=0, ge=0)
    delivered_runs: int = Field(default=0, ge=0)
    cited_runs: int = Field(default=0, ge=0)
    cited_beliefs: int = Field(default=0, ge=0)
    charged_beliefs: int = Field(default=0, ge=0)
    ambiguous_citations: int = Field(default=0, ge=0)
    # Traces.
    traces: int = Field(default=0, ge=0)
    traces_with_lexical: int = Field(default=0, ge=0)
    traces_with_structured: int = Field(default=0, ge=0)
    traces_with_semantic: int = Field(default=0, ge=0)
    p50_recall_latency_ms: float | None = Field(default=None, ge=0)
    p95_recall_latency_ms: float | None = Field(default=None, ge=0)
    slow_traces: int = Field(default=0, ge=0)
    returned: ClassShare = Field(default_factory=ClassShare)
    dropped_for_budget: ClassShare = Field(default_factory=ClassShare)
    # Store composition now.
    live_beliefs: int = Field(default=0, ge=0)
    active_beliefs: int = Field(default=0, ge=0)
    provisional_beliefs: int = Field(default=0, ge=0)
    flagged_beliefs: int = Field(default=0, ge=0)
    owner_authority_beliefs: int = Field(default=0, ge=0)
    never_used_beliefs: int = Field(default=0, ge=0)
    counted_beliefs: int = Field(default=0, ge=0)
    # Lifecycle in the window.
    lifecycle: list[LifecycleCount] = Field(default_factory=list)
    # Semantic index coverage now.
    indexed_live_beliefs: int = Field(default=0, ge=0)
    index_generation: int | None = Field(default=None, ge=0)
    index_state: str | None = None
    index_failed_pages: int | None = Field(default=None, ge=0)

    @property
    def citation_rate(self) -> float | None:
        return self.cited_runs / self.delivered_runs if self.delivered_runs else None

    @property
    def index_coverage(self) -> float | None:
        return self.indexed_live_beliefs / self.live_beliefs if self.live_beliefs else None


def _share(label: str, share: ClassShare) -> str:
    return (
        f"{label}: {share.total} items ({share.flagged} flagged, {share.provisional} provisional, "
        f"{share.source_learned} source-learned, {share.owner_authority} owner-authority)."
    )


def render_memory_health_report(report: MemoryHealthReport) -> str:
    """Format the aggregates for an operator terminal; nothing private is in them."""

    rate = report.citation_rate
    coverage = report.index_coverage
    lines = [
        f"Recall health from {report.since.isoformat()} to {report.until.isoformat()}",
        (
            f"Runs: {report.recalled_runs} recalled, {report.delivered_runs} delivered, "
            f"{report.cited_runs} cited"
            + ("" if rate is None else f" ({rate:.1%} of delivered)")
            + f"; {report.cited_beliefs} citations, {report.charged_beliefs} charged, "
            f"{report.ambiguous_citations} ambiguous."
        ),
        (
            f"Without recall: {report.limited_runs} runs went without automatic recall at "
            f"some step ({report.limited_exhausted} exhausted, {report.limited_timed_out} "
            "timed out)."
        ),
        (
            f"Traces: {report.traces} (lexical {report.traces_with_lexical}, structured "
            f"{report.traces_with_structured}, semantic {report.traces_with_semantic}); "
            f"latency p50 {_ms(report.p50_recall_latency_ms)}, p95 "
            f"{_ms(report.p95_recall_latency_ms)}; {report.slow_traces} over 1s."
        ),
        _share("Returned", report.returned),
        _share("Dropped for budget", report.dropped_for_budget),
        (
            f"Store: {report.live_beliefs} live ({report.active_beliefs} active, "
            f"{report.provisional_beliefs} provisional, {report.flagged_beliefs} flagged, "
            f"{report.owner_authority_beliefs} owner-authority); {report.never_used_beliefs} "
            f"never used; {report.counted_beliefs} with versioned counters."
        ),
        (
            f"Index: {report.indexed_live_beliefs} of {report.live_beliefs} live beliefs "
            "vectorised"
            + ("" if coverage is None else f" ({coverage:.1%})")
            + f"; generation {report.index_generation}, state {report.index_state}, "
            f"failed pages {report.index_failed_pages}."
        ),
    ]
    for item in report.lifecycle:
        floor = "" if item.minimum_confidence is None else f", lowest {item.minimum_confidence:.2f}"
        lines.append(f"  {item.event_type} x{item.count} ({item.authority}{floor})")
    return "\n".join(lines)


def _ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f}ms"
