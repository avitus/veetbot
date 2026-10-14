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
