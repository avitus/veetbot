"""Operator recall-health reads are bounded, database-only and content-free."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_core.cli import main
from agent_core.observability.memory_health import (
    ClassShare,
    LifecycleCount,
    MemoryHealthReport,
    render_memory_health_report,
)

_SINCE = datetime(2026, 10, 1, tzinfo=UTC)


def _report() -> MemoryHealthReport:
    return MemoryHealthReport(
        since=_SINCE,
        until=_SINCE + timedelta(days=14),
        recalled_runs=40,
        delivered_runs=30,
        cited_runs=12,
        cited_beliefs=15,
        charged_beliefs=90,
        ambiguous_citations=1,
        traces=55,
        traces_with_lexical=55,
        traces_with_structured=55,
        traces_with_semantic=0,
        p50_recall_latency_ms=12.0,
        p95_recall_latency_ms=480.0,
        slow_traces=1,
        returned=ClassShare(
            total=300, flagged=40, provisional=200, source_learned=0, owner_authority=50
        ),
        dropped_for_budget=ClassShare(
            total=700, flagged=400, provisional=650, source_learned=55, owner_authority=10
        ),
        live_beliefs=622,
        active_beliefs=53,
        provisional_beliefs=569,
        flagged_beliefs=326,
        owner_authority_beliefs=43,
        never_used_beliefs=600,
        counted_beliefs=22,
        lifecycle=[
            LifecycleCount(
                event_type="memory.retired", authority="affirmed", count=2, minimum_confidence=0.15
            )
        ],
        indexed_live_beliefs=536,
        index_generation=15,
        index_state="complete",
        index_failed_pages=1,
    )


def test_render_reports_shares_rates_and_coverage_without_belief_text() -> None:
    rendered = render_memory_health_report(_report())
    assert "12 cited (40.0% of delivered)" in rendered
    assert (
        "Dropped for budget: 700 items (400 flagged, 650 provisional, 55 source-learned" in rendered
    )
    assert "536 of 622 live beliefs vectorised (86.2%)" in rendered
    assert "memory.retired x2 (affirmed, lowest 0.15)" in rendered
    assert "p95 480ms; 1 over 1s" in rendered
    assert "[m:" not in rendered


def test_citation_rate_and_coverage_are_none_without_denominators() -> None:
    empty = MemoryHealthReport(since=_SINCE, until=_SINCE + timedelta(hours=1))
    assert empty.citation_rate is None and empty.index_coverage is None
    assert "cited;" in render_memory_health_report(empty)


def test_health_command_uses_a_bounded_window_and_prints_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    async def report(hours: int) -> Any:
        calls.append(hours)
        return _report()

    monkeypatch.setattr(main, "operator_memory_health", report)
    result = CliRunner().invoke(main.app, ["memory", "health", "--hours", "336", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["delivered_runs"] == 30
    assert calls == [336]


@pytest.mark.parametrize("hours", ["0", "2161"])
def test_health_window_rejects_unbounded_reads(hours: str) -> None:
    result = CliRunner().invoke(main.app, ["memory", "health", "--hours", hours])
    assert result.exit_code == 2


def test_health_dependency_failure_is_unsuccessful_and_content_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(_hours: int) -> Any:
        raise ConnectionError("postgresql://user:PRIVATE@db/agent")

    monkeypatch.setattr(main, "operator_memory_health", unavailable)
    result = CliRunner().invoke(main.app, ["memory", "health"])
    assert result.exit_code == 1
    assert "PRIVATE" not in result.output and "ConnectionError" in result.output
