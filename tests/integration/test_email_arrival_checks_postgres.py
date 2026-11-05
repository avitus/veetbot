"""PostgreSQL runs a server check through the queue and erases an empty one (ADR-0286)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.application.email import read_value
from agent_core.bootstrap import build
from agent_core.domain.email import EmailTask
from agent_core.domain.errors import NotFoundError
from agent_core.domain.messages import FakeModelScript
from agent_core.domain.runs import RunStatus
from agent_core.runtime.worker import DurableWorker
from tests.gates.test_email_arrival_checks_m26 import FORMATION, _caught_up, _mailbox
from tests.gates.test_email_m18 import _email_settings
from tests.integration.m2_support import database_settings


async def test_an_empty_check_runs_defers_and_is_removed_after_seven_days() -> None:
    clock = FixedClock(datetime.now(UTC))
    settings = replace(
        _email_settings(),
        email_mode_enabled=True,
        database_url=database_settings().database_url,
    )
    async with build(
        settings=settings,
        storage="postgres",
        clock=clock,
        script=FakeModelScript(turns=[]),
        mcp_client_factory=await _mailbox([], []),
    ) as app:
        await _caught_up(app)
        service = app.services.email

        check = await service.admit_arrival_check(app.principal)
        assert check is not None
        # While the check is queued, and then for the rest of its interval, none is due.
        assert await service.admit_arrival_check(app.principal) is None
        worker = cast(DurableWorker, app.async_worker_factory("email-check-test"))
        assert await worker.run_once()

        run = await app.runs.get(check.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        assert run.model_call_count == 0
        task = await service.get_task(app.principal, run.id)
        assert task is not None and task.scope == "arrivals" and task.empty_check is True
        async with app.uow_factory() as uow:
            events = await uow.events.list_after(run.session_id, 0, app.principal, run_id=run.id)
        assert FORMATION not in {event.event_type for event in events}
        assert await service.admit_arrival_check(app.principal) is None

        clock.advance(timedelta(days=8))
        assert await service.prune_empty_checks(app.principal) == 1
        async with app.uow_factory() as uow:
            stored = await read_value(uow.email, app.principal, "task", str(run.id), EmailTask)
            assert stored is None
            with pytest.raises(NotFoundError):
                await uow.sessions.get(run.session_id, app.principal)
