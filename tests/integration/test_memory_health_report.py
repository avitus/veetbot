"""Recall health is measured from durable traces, usage events and the store."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from agent_core.adapters.persistence.database import create_engine, create_session_factory
from agent_core.adapters.persistence.memory_health_report import memory_health_report
from agent_core.bootstrap import build
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from tests.contract.owner_recall_probe_cases import stated
from tests.integration.m2_support import database_settings
from tests.integration.test_chat_latency_report import _complete

pytestmark = pytest.mark.integration


async def test_health_report_counts_delivery_usage_store_and_index() -> None:
    settings = database_settings()
    script = FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last")
    async with build(settings=settings, storage="postgres", script=script) as app:
        session_id = await app.sessions.create()
        stated_belief = await stated(
            app, session_id, "User prefers concise answers", "answer style"
        )
        script.turns[0] = ScriptedTurn(text=f"Briefly, per [m:{str(stated_belief.id)[:8]}].")
        await _complete(app, "How should answers be shaped?")
        until = app.clock.now() + timedelta(seconds=1)
        engine = create_engine(settings.database_url)
        try:
            report = await memory_health_report(
                create_session_factory(engine),
                until - timedelta(hours=1),
                until,
                principal=app.principal,
            )
        finally:
            await engine.dispose()

    assert report.recalled_runs >= 1
    assert report.delivered_runs >= 1
    assert report.traces >= 1 and report.traces_with_lexical >= 1
    assert report.live_beliefs >= 1 and report.owner_authority_beliefs >= 1
    assert report.returned.total >= 1 and report.returned.owner_authority >= 1
    assert report.p50_recall_latency_ms is not None
    assert report.indexed_live_beliefs == 0 and report.index_generation is None
    # Counters only move for delivered beliefs; the cited one is counted.
    assert report.cited_runs == 1 and report.counted_beliefs >= 1
    assert "concise" not in report.model_dump_json()


async def test_health_report_reads_the_trace_summary_not_the_trace_document() -> None:
    """Production regression: aggregates over trace documents passed the statement bound.

    A trace document is tens of kilobytes, so the report may read only the
    small summary stored beside it. Emptying every document proves that: the
    trace figures must survive, because none of them comes from a document.
    """

    settings = database_settings()
    script = FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last")
    async with build(settings=settings, storage="postgres", script=script) as app:
        session_id = await app.sessions.create()
        await stated(app, session_id, "User prefers concise answers", "answer style")
        await _complete(app, "How should answers be shaped?")
        until = app.clock.now() + timedelta(seconds=1)
        engine = create_engine(settings.database_url)
        try:
            async with engine.begin() as connection:
                emptied = await connection.execute(
                    text(
                        "update recall_traces set trace = '{}'::jsonb "
                        "where tenant_id = :tenant and principal_id = :principal"
                    ),
                    {
                        "tenant": app.principal.tenant_id,
                        "principal": app.principal.principal_id,
                    },
                )
            report = await memory_health_report(
                create_session_factory(engine),
                until - timedelta(hours=1),
                until,
                principal=app.principal,
            )
        finally:
            await engine.dispose()

    assert emptied.rowcount >= 1
    assert report.traces >= 1
    assert report.traces_with_lexical >= 1 and report.traces_with_structured >= 1
    assert report.returned.total >= 1 and report.returned.owner_authority >= 1
    assert report.p50_recall_latency_ms is not None
    assert report.model_dump().get("summarized_traces") == report.traces


async def test_health_report_counts_unsummarized_traces_apart() -> None:
    """A trace recorded before the summary existed is counted, never read."""

    settings = database_settings()
    script = FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last")
    async with build(settings=settings, storage="postgres", script=script) as app:
        session_id = await app.sessions.create()
        await stated(app, session_id, "User prefers concise answers", "answer style")
        await _complete(app, "How should answers be shaped?")
        until = app.clock.now() + timedelta(seconds=1)
        engine = create_engine(settings.database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "update recall_traces set summary = null "
                        "where tenant_id = :tenant and principal_id = :principal"
                    ),
                    {
                        "tenant": app.principal.tenant_id,
                        "principal": app.principal.principal_id,
                    },
                )
            report = await memory_health_report(
                create_session_factory(engine),
                until - timedelta(hours=1),
                until,
                principal=app.principal,
            )
        finally:
            await engine.dispose()

    assert report.traces >= 1 and report.model_dump().get("summarized_traces") == 0
    assert report.returned.total == 0 and report.p50_recall_latency_ms is None


async def test_health_report_rejects_unbounded_or_naive_windows() -> None:
    settings = database_settings()
    async with build(settings=settings, storage="postgres") as app:
        engine = create_engine(settings.database_url)
        try:
            until = app.clock.now()
            with pytest.raises(ValueError, match="ninety days"):
                await memory_health_report(
                    create_session_factory(engine),
                    until - timedelta(days=91),
                    until,
                    principal=app.principal,
                )
        finally:
            await engine.dispose()
