"""`agent run latency` is a reserved, read-only report command (ADR-0131)."""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_core.cli import main
from agent_core.observability.latency import (
    ChatLatencyReport,
    Distribution,
    ModelTiming,
    NamedDistribution,
    QueueWait,
)

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)


def _report(since: datetime) -> ChatLatencyReport:
    return ChatLatencyReport(
        since=since,
        turns=80,
        phases=[
            NamedDistribution(name="turn", count=80, p50=29.16, p90=73.87, maximum=174.75),
            NamedDistribution(name="setup", count=80, p50=7.87, p90=14.61, maximum=25.53),
        ],
        setup=[NamedDistribution(name="first turn", count=45, p50=11.09, p90=17.19, maximum=25.53)],
        models=[
            ModelTiming(
                model="openai/gpt-6-astra",
                reasoning_effort="high",
                calls=19,
                duration=Distribution(count=19, p50=5.6, p90=17.7, maximum=20.0),
                first_text=Distribution(count=4, p50=3.1, p90=9.0, maximum=9.5),
                average_input_tokens=9480,
                average_output_tokens=259,
                cached_input_percent=52.9,
            )
        ],
        tools=[NamedDistribution(name="web.fetch", count=79, p50=0.9, p90=4.29, maximum=30.18)],
        mcp_connections=[
            NamedDistribution(name="gmail_read", count=3, p50=2.1, p90=3.0, maximum=3.2)
        ],
        mcp_pins_reused=40,
        queue=[
            QueueWait(
                priority=0,
                session_kind="chat",
                wait=Distribution(count=88, p50=0.24, p90=0.35, maximum=0.4),
            )
        ],
        cached_input_percent=27.8,
        reasoning_output_percent=12.8,
    )


class _Clock:
    def now(self) -> datetime:
        return NOW


@dataclass
class _Composition:
    latency_report: Callable[[datetime], Awaitable[ChatLatencyReport]] | None
    clock: _Clock


def _serve(
    monkeypatch: pytest.MonkeyPatch,
    report: Callable[[datetime], Awaitable[ChatLatencyReport]] | None,
    seen: list[dict[str, Any]],
) -> None:
    @asynccontextmanager
    async def build(**options: Any) -> AsyncIterator[_Composition]:
        seen.append(options)
        yield _Composition(latency_report=report, clock=_Clock())

    monkeypatch.setattr(main, "build", build)


def test_latency_is_a_reserved_run_word_and_reads_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[datetime] = []
    seen: list[dict[str, Any]] = []

    async def report(since: datetime) -> ChatLatencyReport:
        requested.append(since)
        return _report(since)

    _serve(monkeypatch, report, seen)

    result = CliRunner().invoke(main.app, ["run", "latency", "--days", "7"])

    assert result.exit_code == 0, result.output
    assert seen == [{"storage": "postgres"}]
    assert requested == [datetime(2026, 9, 18, 12, tzinfo=UTC)]
    assert "80 Chat turns since 2026-09-18" in result.output
    assert "setup" in result.output and "7.87" in result.output
    assert "openai/gpt-6-astra (high)" in result.output
    assert "web.fetch" in result.output
    assert "27.8%" in result.output


def test_latency_json_is_the_whole_report(monkeypatch: pytest.MonkeyPatch) -> None:
    async def report(since: datetime) -> ChatLatencyReport:
        return _report(since)

    _serve(monkeypatch, report, [])

    result = CliRunner().invoke(main.app, ["run", "latency", "--json"])

    assert result.exit_code == 0, result.output
    decoded = json.loads(result.output)
    assert decoded["turns"] == 80
    assert decoded["since"].startswith("2026-09-18")
    assert decoded["models"][0]["cached_input_percent"] == 52.9


def test_latency_refuses_storage_without_the_event_log(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, None, [])

    result = CliRunner().invoke(main.app, ["run", "latency"])

    assert result.exit_code == 1
    assert "PostgreSQL" in result.output


@pytest.mark.parametrize("days", ["0", "91"])
def test_latency_window_is_bounded(monkeypatch: pytest.MonkeyPatch, days: str) -> None:
    _serve(monkeypatch, None, [])

    result = CliRunner().invoke(main.app, ["run", "latency", "--days", days])

    assert result.exit_code == 2
