"""A completed top-level reply leaves a title request; nothing else does (ADR-0155)."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.bootstrap import build
from agent_core.domain.messages import FakeModelScript, ModelPermanentError, ScriptedTurn
from agent_core.domain.runs import RunStatus
from agent_core.runtime.worker import MaintenanceWorker
from tests.contract.support import NOW
from tests.integration.m2_support import memory_settings


async def test_a_completed_reply_requests_a_title_without_reordering_the_sidebar() -> None:
    async with build(
        settings=memory_settings(),
        script=FakeModelScript(turns=[ScriptedTurn(text="Here is a planting plan.")]),
    ) as app:
        run_id = await app.runs.submit("can you help with the garden")
        run = await app.runs.wait_terminal(run_id)
        assert run.status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            before = (await uow.sessions.get(run.session_id, app.principal)).updated_at
            pending = await uow.sessions.pending_title_requests(app.principal, limit=10)
            after = (await uow.sessions.get(run.session_id, app.principal)).updated_at

    assert [request.session_id for request in pending] == [run.session_id]
    assert pending[0].title == "can you help with the garden"
    assert before == after


async def test_a_failed_run_requests_nothing() -> None:
    failed = ScriptedTurn(
        fail_with=ModelPermanentError(
            provider="fake",
            model="scripted",
            attempt_id=UUID(int=0),
            message="The model provider failed.",
            http_status=500,
        )
    )
    async with build(settings=memory_settings(), script=FakeModelScript(turns=[failed])) as app:
        run_id = await app.runs.submit("can you help with the garden")
        run = await app.runs.wait_terminal(run_id)
        assert run.status is RunStatus.FAILED
        async with app.uow_factory() as uow:
            assert await uow.sessions.pending_title_requests(app.principal, limit=10) == []


async def test_the_maintenance_role_titles_a_completed_conversation() -> None:
    # The shipped profile routes titles to `balanced`; its provider is scripted here.
    title_model = FakeModelProvider(
        FakeModelScript(
            turns=[ScriptedTurn(text='{"decision": "replace", "title": "Shade garden planting"}')]
        ),
        FixedClock(NOW),
    )
    async with build(
        settings=memory_settings(),
        script=FakeModelScript(turns=[ScriptedTurn(text="Here is a planting plan.")]),
        model_provider_overrides={"openai": title_model},
    ) as app:
        run_id = await app.runs.submit("can you help with the garden")
        run = await app.runs.wait_terminal(run_id)
        maintenance = cast(MaintenanceWorker, app.maintenance_factory())
        await maintenance.run_once()
        async with app.uow_factory() as uow:
            titled = await uow.sessions.get(run.session_id, app.principal)
            pending = await uow.sessions.pending_title_requests(app.principal, limit=10)

    assert titled.title == "Shade garden planting"
    assert pending == []
    assert title_model.requests[0].metadata["purpose"] == "conversation_title"
