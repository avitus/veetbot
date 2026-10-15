"""Operational alerts are atomic, scoped, deduplicated and recoverable."""

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import text

from agent_core.adapters.persistence.database import create_engine
from agent_core.bootstrap import build
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.observability.reliability import ReliabilityThresholds
from tests.integration.m2_support import database_settings
from tests.integration.test_chat_latency_report import _complete

pytestmark = pytest.mark.integration


async def test_reliability_monitor_deduplicates_concurrent_checks_and_records_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = replace(
        database_settings(), notification_api_enabled=True, notification_dispatch_enabled=True
    )
    async with build(
        settings=settings,
        storage="postgres",
        script=FakeModelScript(turns=[ScriptedTurn(text="Done")]),
    ) as app:
        check = getattr(app, "reliability_check", None)
        assert check is not None, "Reliability checks need a durable, opt-in alarm path."
        await _complete(app, "Synthetic task")
        thresholds = ReliabilityThresholds(minimum_outcomes=1, internal_errors=1)
        engine = create_engine(settings.database_url)
        try:

            async def status(value: str) -> None:
                async with engine.begin() as connection:
                    await connection.execute(
                        text("update runs set status=:status, failure=cast(:failure as jsonb)"),
                        {
                            "status": value,
                            "failure": json.dumps(
                                {"reason": "internal_error", "error_class": "RuntimeError"}
                            ),
                        },
                    )

            async def kinds() -> list[str]:
                async with engine.connect() as connection:
                    return list(
                        (
                            await connection.execute(
                                text(
                                    "select kind from notification_outbox "
                                    "where kind in ('ops_alert','ops_recovered')"
                                )
                            )
                        ).scalars()
                    )

            await status("FAILED")
            from agent_core.adapters.persistence.repositories import PostgresProcessEventRepository

            async def broken_append(*_args: Any, **_kwargs: Any) -> None:
                raise RuntimeError("Synthetic transaction failure")

            with monkeypatch.context() as patch:
                patch.setattr(PostgresProcessEventRepository, "append", broken_append)
                with pytest.raises(RuntimeError, match="Synthetic transaction failure"):
                    await check(1, thresholds)
            assert await kinds() == [], "A failed transition must roll back its notification."
            await asyncio.gather(check(1, thresholds), check(1, thresholds))
            assert (await kinds()).count("ops_alert") == 2
            await status("CANCELLED")
            await check(1, thresholds)
            assert (await kinds()).count("ops_recovered") == 0
            await status("COMPLETED")
            await asyncio.gather(check(1, thresholds), check(1, thresholds))
            assert (await kinds()).count("ops_recovered") == 2
            await status("FAILED")
            await check(1, thresholds)
            assert (await kinds()).count("ops_alert") == 4
            async with app.uow_factory() as uow:
                events = await uow.process_events.list("ops.reliability.transition")
            assert len(events) == 6
            assert all("Synthetic" not in event.model_dump_json() for event in events)
        finally:
            await engine.dispose()
