"""PostgreSQL binds judgment reservation, receipt, and email settlement atomically."""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from agent_core.adapters.judgment import FakeJudgmentProvider
from agent_core.bootstrap import build
from agent_core.domain.judgment import JudgmentFailure, JudgmentProviderError
from agent_core.domain.messages import FakeModelScript
from agent_core.domain.runs import RunStatus
from agent_core.runtime.worker import DurableWorker
from tests.gates.test_email_runtime_m26 import _assessment_turn, _current_mail_factory
from tests.integration.m2_support import database_settings
from tests.unit.test_email_junk_first_pass import decision, filter_settings


@pytest.mark.parametrize("uncertain", [False, True])
async def test_junk_usage_and_uncertain_settlement_survive_database_reload(
    tmp_path: Path, uncertain: bool
) -> None:
    """Verify durable receipts distinguish actual usage from reserved unknown costs."""
    settings = replace(filter_settings(tmp_path), database_url=database_settings().database_url)
    judge = FakeJudgmentProvider(
        [
            JudgmentProviderError(JudgmentFailure.PROVIDER_UNAVAILABLE, retryable=True)
            if uncertain
            else decision()
        ]
    )
    async with build(
        settings=settings,
        storage="postgres",
        judgment_provider_override=judge,
        script=FakeModelScript(turns=[_assessment_turn()] if uncertain else []),
        mcp_client_factory=await _current_mail_factory(),
    ) as app:
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        worker = cast(DurableWorker, app.async_worker_factory("email-junk-test"))
        assert await worker.run_once()
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        task = await app.services.email.get_task(app.principal, operation.run_id)
        assert task
        if uncertain:
            assert task.settled_cost is None and task.budget_charge == Decimal("1")
        else:
            assert task.settled_cost == decision().usage.cost == run.usage.cost
        async with app.uow_factory() as uow:
            events = await uow.events.list_after(run.session_id, 0, app.principal, run_id=run.id)
        assert [e.event_type for e in events if e.event_type.startswith("email.junk.")] == [
            "email.junk.started",
            "email.junk.completed",
        ]


async def test_pilot_limit_survives_concurrent_saves_and_reload(tmp_path: Path) -> None:
    """PostgreSQL serializes the daily cap and preserves idempotency across fresh units of work."""

    from tests.unit.test_email_junk_pilot import (
        assert_durable_pilot_cap,
        pilot_decision,
        pilot_settings,
        work_factory,
    )

    settings = replace(pilot_settings(tmp_path), database_url=database_settings().database_url)
    async with build(
        settings=settings,
        storage="postgres",
        judgment_provider_override=FakeJudgmentProvider([pilot_decision()]),
        script=FakeModelScript(turns=[]),
        mcp_client_factory=await work_factory(),
    ) as app:
        operation = await app.services.email.submit_task(app.principal, kind="refresh")
        worker = cast(DurableWorker, app.async_worker_factory("email-junk-pilot-test"))
        assert await worker.run_once()
        run = await app.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        await assert_durable_pilot_cap(app)


async def test_failed_pilot_save_rolls_back_slot_and_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed assessment commit cannot consume a slot or leave a partial bypass behind."""
    from typing import Any
    from uuid import uuid4

    from agent_core.application.email import save_value
    from agent_core.domain.email import EmailThread
    from agent_core.domain.email_junk import JunkPilot
    from tests.unit.test_email_junk_pilot import (
        pilot_decision,
        pilot_packet,
        pilot_settings,
        work_factory,
    )

    settings = replace(pilot_settings(tmp_path), database_url=database_settings().database_url)
    async with build(
        settings=settings,
        storage="postgres",
        judgment_provider_override=FakeJudgmentProvider([pilot_decision()]),
        script=FakeModelScript(turns=[]),
        mcp_client_factory=await work_factory(),
    ) as app:
        await app.services.email.submit_task(app.principal, kind="refresh")
        assert await cast(
            DurableWorker, app.async_worker_factory("email-pilot-rollback")
        ).run_once()
        async with app.uow_factory() as uow, uow.email.lock(app.principal):
            [source] = await uow.email.list(app.principal, "thread")
            [assessment] = await uow.email.list(app.principal, "assessment")
            thread = EmailThread.model_validate(source.payload).model_copy(
                update={"id": uuid4(), "provider_thread_id": "rollback-case", "summary": "before"}
            )
            await save_value(
                uow.email, app.principal, "thread", str(thread.id), thread, app.clock.now()
            )
        service = app.services.email
        original = type(service)._put_data

        async def fail(
            self: Any, store: Any, principal: Any, kind: str, key: str, payload: Any
        ) -> Any:
            """Abort after the quota and thread updates but before the assessment write."""
            if kind == "assessment":
                raise RuntimeError("simulated failed commit")
            return await original(self, store, principal, kind, key, payload)

        monkeypatch.setattr(type(service), "_put_data", fail)
        with pytest.raises(RuntimeError, match="simulated failed commit"):
            await service.save_assessment(
                app.principal,
                thread.id,
                thread.revision,
                assessment.payload,
                junk_pilot=JunkPilot.model_validate(pilot_packet()),
            )
        async with app.uow_factory() as uow:
            [counter] = await uow.email.list(app.principal, "junk_pilot_day")
            assert counter.payload == {"count": 1}
            assert len(await uow.email.list(app.principal, "junk_pilot_admission")) == 1
            assert await uow.email.get(app.principal, "assessment", str(thread.id)) is None
            stored = await uow.email.get(app.principal, "thread", str(thread.id))
            assert stored and stored.payload["summary"] == "before"
