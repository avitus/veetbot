"""An owner's unsubscribe gesture completes through the durable queue and a separate worker."""

from dataclasses import replace
from typing import cast
from uuid import UUID

from agent_core.adapters.models.fake import FakeModelScript
from agent_core.bootstrap import build
from agent_core.domain.runs import RunStatus
from agent_core.runtime.worker import DurableWorker
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_unsubscribe_m31 import (
    URL,
    Mailbox,
    Transport,
    _assessment_turn,
    _client,
    _rows,
    _shop,
    _target,
)
from tests.integration.m2_support import database_settings


async def test_a_queued_unsubscribe_gesture_reaches_the_sender_once() -> None:
    """Production claims the gesture from the queue; the tap must still be its approval."""
    mailbox, transport = Mailbox(), Transport()
    _shop(mailbox)
    settings = replace(
        _email_settings(),
        database_url=database_settings().database_url,
        email_mode_enabled=True,
        email_unsubscribe_enabled=True,
    )
    async with build(
        settings=settings,
        storage="postgres",
        mcp_client_factory=await mailbox.factory(),
        one_click_transport_override=transport,
        script=FakeModelScript(turns=[_assessment_turn() for _ in range(8)]),
    ) as app:
        worker = cast(DurableWorker, app.worker_factory("interactive-lane"))
        background = cast(DurableWorker, app.async_worker_factory("async-lane"))
        refresh = await app.services.email.submit_task(app.principal, kind="refresh")
        for _ in range(6):
            if (await app.runs.get(refresh.run_id)).status is RunStatus.COMPLETED:
                break
            await background.run_once()
            await worker.run_once()
        refreshed = await app.runs.get(refresh.run_id)
        assert refreshed.status is RunStatus.COMPLETED, (refreshed.status, refreshed.failure)
        [row] = await _rows(app)
        async with _client(app) as client:
            body = {"targets": [_target(row)], "archive_existing": False, "idempotency_key": "t1"}
            result = await client.post("/v1/email/subscriptions/unsubscribe", json=body)
        assert result.status_code == 200, result.text
        run_id = UUID(result.json()["run_id"])
        for _ in range(6):
            if (await app.runs.get(run_id)).status is RunStatus.COMPLETED:
                break
            await worker.run_once()
        run = await app.runs.get(run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        [after] = await _rows(app)
        assert after["operation"]["code"] == "unsubscribe.accepted", after["operation"]
        assert transport.posts == [URL]
