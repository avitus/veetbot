"""Reliability is measured from durable, scoped outcomes, never private payloads."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import text

from agent_core.adapters.persistence.database import create_engine
from agent_core.bootstrap import build
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from tests.integration.m2_support import database_settings
from tests.integration.test_chat_latency_report import _complete

pytestmark = pytest.mark.integration


async def test_reliability_counts_terminal_outcomes_and_excludes_foreign_principals() -> None:
    """Verify that reliability counts terminal outcomes and excludes foreign principals."""
    settings = database_settings()
    async with build(
        settings=settings,
        storage="postgres",
        script=FakeModelScript(
            turns=[ScriptedTurn(text="Private answer")], on_exhausted="repeat_last"
        ),
    ) as app:
        report = getattr(app, "reliability_report", None)
        assert report is not None, "Production needs a durable reliability report."
        for _ in range(4):
            await _complete(app, "Private prompt")
        engine = create_engine(settings.database_url)
        try:
            async with engine.begin() as connection:
                rows = (
                    await connection.execute(
                        text("select id, session_id from runs order by created_at")
                    )
                ).all()
                await connection.execute(
                    text(
                        "update runs set status='FAILED', "
                        "failure=cast(:failure as jsonb) where id=:id"
                    ),
                    {
                        "id": rows[1].id,
                        "failure": json.dumps(
                            {
                                "reason": "internal_error",
                                "error_class": "ValueError",
                                "message": "PRIVATE_EXCEPTION",
                                "details": {
                                    "error_fingerprint": "a" * 64,
                                    "release_id": "test-release",
                                },
                            }
                        ),
                    },
                )
                await connection.execute(
                    text("update runs set status='CANCELLED' where id=:id"), {"id": rows[2].id}
                )
                await connection.execute(
                    text("update sessions set principal_id='another-owner' where id=:id"),
                    {"id": rows[3].session_id},
                )
            now = app.clock.now()
            result = await report(now - timedelta(hours=24), now)
            assert result.completed == 1 and result.failed == 1 and result.cancelled == 1
            assert result.completion_rate == 0.5
            assert result.failures[0].error_class == "ValueError"
            assert result.failures[0].fingerprint == "a" * 64
            assert result.failures[0].release_id == "test-release"
            rendered = result.model_dump_json()
            assert "PRIVATE_EXCEPTION" not in rendered and "Private" not in rendered
            empty = await report(now + timedelta(hours=1), now + timedelta(hours=2))
            assert empty.completion_rate is None
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "update runs set status='QUEUED', created_at=:created, "
                        "scheduled_for=:future where id=:id"
                    ),
                    {
                        "id": rows[0].id,
                        "created": now - timedelta(hours=2),
                        "future": now + timedelta(hours=1),
                    },
                )
                await connection.execute(
                    text(
                        "update runs set status='QUEUED', created_at=:created, "
                        "updated_at=:created, scheduled_for=null where id=:id"
                    ),
                    {"id": rows[1].id, "created": now - timedelta(seconds=130)},
                )
                await connection.execute(
                    text("update runs set status='WAITING_FOR_USER' where id=:id"),
                    {"id": rows[2].id},
                )
                await connection.execute(
                    text(
                        "update runs set status='RUNNING', lease_expires_at=:expired where id=:id"
                    ),
                    {"id": rows[3].id, "expired": now - timedelta(seconds=1)},
                )
            queue = await report(now - timedelta(hours=24), now)
            assert queue.queued == 2 and queue.waiting == 1
            assert queue.running == 0 and queue.stale_leases == 0
            assert queue.oldest_ready_queue_seconds == pytest.approx(130)
            assert queue.completion_rate is None
        finally:
            await engine.dispose()


async def test_requeued_run_reports_age_since_it_became_ready_not_since_creation() -> None:
    """Verify that a run requeued after a long wait reports seconds of queue age, not hours."""
    settings = database_settings()
    async with build(
        settings=settings,
        storage="postgres",
        script=FakeModelScript(turns=[ScriptedTurn(text="Answer")], on_exhausted="repeat_last"),
    ) as app:
        report = app.reliability_report
        assert report is not None
        await _complete(app, "Prompt")
        engine = create_engine(settings.database_url)
        try:
            now = app.clock.now()
            async with engine.begin() as connection:
                # An approval, user answer or child join requeues through a
                # status transition that keeps created_at and a null scheduled_for.
                await connection.execute(
                    text(
                        "update runs set status='QUEUED', created_at=:created, "
                        "updated_at=:requeued, scheduled_for=null"
                    ),
                    {"created": now - timedelta(hours=6), "requeued": now - timedelta(seconds=5)},
                )
            queue = await report(now - timedelta(hours=24), now)
            assert queue.queued == 1
            assert queue.oldest_ready_queue_seconds == pytest.approx(5)
        finally:
            await engine.dispose()


async def test_operator_report_does_not_bootstrap_providers_or_write_application_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that operator report does not bootstrap providers or write application state."""
    import agent_core.bootstrap as bootstrap
    from agent_core.cli import main
    from agent_core.observability.reliability import ReliabilityThresholds

    monkeypatch.setattr(bootstrap, "load_settings", database_settings)

    def forbidden_build(**_kwargs: object) -> None:
        """Reject composition startup during a database-only operator report."""
        raise AssertionError("An operator report must not bootstrap the application.")

    monkeypatch.setattr(main, "build", forbidden_build)
    result = await main._reliability_report(1, False, ReliabilityThresholds())
    assert result.completed == 0 and result.completion_rate is None
