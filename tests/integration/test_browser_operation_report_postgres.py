"""Browser reports read only owner-scoped, content-free execution diagnostics."""

from datetime import timedelta

import pytest

from agent_core.bootstrap import build
from tests.integration.m2_support import database_settings

pytestmark = pytest.mark.integration


async def test_browser_report_is_composed_and_empty_traffic_is_not_success() -> None:
    """A PostgreSQL operator can inspect browser evidence without inventing traffic."""
    async with build(settings=database_settings(), storage="postgres") as app:
        report = getattr(app, "browser_report", None)
        assert report is not None, "W2 requires a read-only browser operation report."
        now = app.clock.now()
        result = await report(now - timedelta(hours=24), now)
    assert result.operations == 0
    assert result.valid_diagnostics == 0
    assert result.phases == []
    assert result.operation_timings == []


async def test_browser_report_excludes_foreign_owners_and_private_event_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope, byte limits, malformed records and windows survive a real PostgreSQL projection."""
    import json

    from sqlalchemy import text

    from agent_core.adapters.persistence.database import create_engine
    from agent_core.observability.browser import render_browser_report

    settings = database_settings()
    async with build(settings=settings, storage="postgres") as app:
        assert app.browser_report is not None
        for _ in range(10):
            await app.runs.submit("PRIVATE_PROMPT_CANARY")
        now = app.clock.now()
        valid = {
            "version": 1,
            "elapsed_ms": 1500,
            "truncated": False,
            "phases": [
                {
                    "phase": "navigation",
                    "placement": "runtime",
                    "outcome": "completed",
                    "elapsed_ms": 1000,
                }
            ],
        }
        engine = create_engine(settings.database_url)
        try:
            async with engine.begin() as connection:
                runs = (
                    await connection.execute(
                        text("select id, session_id from runs order by created_at, id")
                    )
                ).all()
                for index, run in enumerate(runs):
                    payload = {
                        "name": "browser.navigate",
                        "browser_diagnostics": valid,
                        "arguments": {"url": "PRIVATE_PAGE_CANARY"},
                        "result_item": {"text": "PRIVATE_RESULT_CANARY"},
                    }
                    event_type = "tool.call.completed"
                    if index == 1:
                        event_type = "tool.call.uncertain"
                        payload["browser_diagnostics"] = {**valid, "truncated": True}
                    if index == 2:
                        del payload["browser_diagnostics"]
                    if index == 3:
                        payload["browser_diagnostics"] = {
                            **valid,
                            "secret": "PRIVATE_DIAGNOSTIC_CANARY",
                        }
                    if index == 4:
                        payload["browser_diagnostics"] = {**valid, "version": 2}
                    if index == 5:
                        payload["browser_diagnostics"] = {
                            "secret": "PRIVATE_OVERSIZE_CANARY" * 1000
                        }
                    if index == 6:
                        event_type = "tool.call.denied"
                    await connection.execute(
                        text(
                            "insert into events (session_id,run_id,sequence,event_type,"
                            "payload_schema_version,actor_type,payload,created_at) "
                            "select :session,:run,coalesce(max(sequence),0)+1,:event,1,'tool',"
                            "cast(:payload as jsonb),:created "
                            "from events where session_id=:session"
                        ),
                        {
                            "session": run.session_id,
                            "run": run.id,
                            "event": event_type,
                            "payload": json.dumps(payload),
                            "created": now - timedelta(days=2) if index == 9 else now,
                        },
                    )
                await connection.execute(
                    text("update sessions set principal_id='another-owner' where id=:id"),
                    {"id": runs[7].session_id},
                )
                await connection.execute(
                    text("update sessions set tenant_id='another-tenant' where id=:id"),
                    {"id": runs[8].session_id},
                )
                before = (
                    await connection.execute(text("select count(*) from events"))
                ).scalar_one()
            report = await app.browser_report(now - timedelta(hours=24), now)
            from agent_core.adapters.persistence import browser_report as persistence
            from agent_core.observability import browser as aggregates

            with monkeypatch.context() as bounded:
                bounded.setattr(persistence, "MAX_BROWSER_REPORT_OPERATIONS", 2)
                bounded.setattr(aggregates, "MAX_BROWSER_REPORT_OPERATIONS", 2)
                capped = await app.browser_report(now - timedelta(hours=24), now)
            assert capped.operations == 2 and capped.selection_truncated
            # Ordering uses timestamp then event identity: these newest records are invalid.
            assert capped.invalid_diagnostics == 2
            async with engine.connect() as connection:
                assert (
                    await connection.execute(text("select count(*) from events"))
                ).scalar_one() == before
        finally:
            await engine.dispose()
    assert report.operations == 6
    assert report.valid_diagnostics == 2
    assert report.missing_diagnostics == 1
    assert report.invalid_diagnostics == 3
    assert report.truncated_diagnostics == 1
    assert {item.outcome: item.count for item in report.outcomes} == {
        "completed": 5,
        "uncertain": 1,
    }
    assert report.phases[0].count == 2
    assert report.phases[0].p50_seconds == 1
    assert "PRIVATE_" not in report.model_dump_json() + render_browser_report(report)
    assert "another-owner" not in report.model_dump_json()


@pytest.mark.parametrize("kind", ["naive", "reversed", "too-long"])
async def test_browser_report_refuses_invalid_windows(kind: str) -> None:
    """The persistence boundary refuses unbounded or ambiguous windows independently of the CLI."""
    async with build(settings=database_settings(), storage="postgres") as app:
        assert app.browser_report is not None
        until = app.clock.now()
        since = until - timedelta(hours=1)
        if kind == "naive":
            since = since.replace(tzinfo=None)
        elif kind == "reversed":
            since = until + timedelta(hours=1)
        else:
            since = until - timedelta(days=91)
        with pytest.raises(ValueError, match="windows must be aware"):
            await app.browser_report(since, until)


async def test_browser_report_transaction_rejects_an_accidental_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostgreSQL itself refuses writes even if the report query is accidentally changed."""
    from sqlalchemy.exc import DBAPIError

    from agent_core.adapters.persistence import browser_report
    from agent_core.adapters.persistence.database import create_engine, create_session_factory
    from tests.integration.m2_support import PRINCIPAL

    engine = create_engine(database_settings().database_url)
    monkeypatch.setattr(
        browser_report, "_SAMPLES", "CREATE TABLE browser_report_write_probe (value integer)"
    )
    try:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        with pytest.raises(DBAPIError, match="read-only"):
            await browser_report.browser_operation_report(
                create_session_factory(engine), now - timedelta(hours=1), now, principal=PRINCIPAL
            )
    finally:
        await engine.dispose()
