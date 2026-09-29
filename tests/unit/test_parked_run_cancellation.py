"""Cancelling a run nobody holds ends it directly and answers what it was waiting on.

http-api-and-streaming.md, "Cancellation across a process boundary": a queued
or WAITING_* run transitions directly to CANCELLED because no worker holds its
lease, while a RUNNING run only records cancel_requested_at for its worker to
observe. A cancelled question or delegation must not be left looking
answerable, so the suspended invocation fails with a platform-authored result.
Both the owner's service and the surface process's state writer do this.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.bootstrap import Composition, build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.approvals import ApprovalStatus
from agent_core.domain.events import EventEnvelope
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import RunStatus
from agent_core.domain.tools import ToolInvocation, ToolInvocationStatus, ToolOutcomeStatus
from agent_core.runtime.executor import SurfaceRunStateWriter
from tests.contract.support import NOW


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/unused",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials=MappingProxyType({}),
        interpolation=MappingProxyType({"OPENAI_MODEL": ""}),
        artifact_root=tmp_path / "artifacts",
    )


def _suspending_script(tool_name: str, arguments: dict[str, str]) -> FakeModelScript:
    return FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[ScriptedToolCall(name=tool_name, arguments=arguments, call_id="park")],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="never reached", stop_reason=StopReason.END_TURN),
        ]
    )


def _ask_user() -> FakeModelScript:
    return _suspending_script("conversation.ask_user", {"question": "Which region?"})


def _external_write() -> FakeModelScript:
    return _suspending_script("demo.external_write", {"destination": "demo", "content": "hello"})


async def _state(
    app: Composition, run_id: object
) -> tuple[list[ToolInvocation], list[EventEnvelope]]:
    run = await app.runs.get(run_id)  # type: ignore[arg-type]
    async with app.uow_factory() as uow:
        invocations = await uow.invocations.list_for_run(run.id, app.principal)
        events = await uow.events.list_after(run.session_id, 0, app.principal)
    return invocations, [event for event in events if event.run_id == run.id]


def _assert_question_failed_by_cancellation(invocation: ToolInvocation) -> None:
    assert invocation.tool_name == "conversation.ask_user"
    assert invocation.status is ToolInvocationStatus.FAILED
    assert invocation.suspended_kind is None
    assert invocation.outcome is not None
    assert invocation.outcome.status is ToolOutcomeStatus.FAILED
    assert invocation.outcome.reason_code == "tool.run_cancelled"
    assert invocation.result_item is not None
    assert invocation.result_item.is_error is True
    assert invocation.result_item.trust is TrustLevel.PLATFORM
    assert invocation.result_item.call_id == invocation.call_id


async def test_cancelling_a_run_waiting_for_the_user_ends_it_and_fails_the_question(
    tmp_path: Path,
) -> None:
    async with build(
        settings=_settings(tmp_path), script=_ask_user(), clock=FixedClock(NOW)
    ) as app:
        run_id = await app.runs.submit("Choose a region.")
        assert (await app.runs.get(run_id)).status is RunStatus.WAITING_FOR_USER

        cancelled = await app.services.runs.cancel(app.principal, run_id)
        again = await app.services.runs.cancel(app.principal, run_id)
        invocations, events = await _state(app, run_id)

    assert cancelled.accepted is False
    assert cancelled.run.status is RunStatus.CANCELLED
    assert again.accepted is False
    assert again.run.status is RunStatus.CANCELLED
    [question] = invocations
    _assert_question_failed_by_cancellation(question)
    cancellations = [event for event in events if event.event_type == "run.cancelled"]
    assert [(event.actor_type, event.payload) for event in cancellations] == [
        ("principal", {"reason": "requested"})
    ]


async def test_the_composition_run_service_ends_a_run_waiting_for_the_user(
    tmp_path: Path,
) -> None:
    """The in-process RunService (CLI and eval runner) follows the same cancel table."""
    async with build(
        settings=_settings(tmp_path), script=_ask_user(), clock=FixedClock(NOW)
    ) as app:
        run_id = await app.runs.submit("Choose a region.")
        assert (await app.runs.get(run_id)).status is RunStatus.WAITING_FOR_USER

        cancelled = await app.runs.cancel(run_id)
        again = await app.runs.cancel(run_id)
        invocations, _ = await _state(app, run_id)

    assert cancelled.status is RunStatus.CANCELLED
    assert again.status is RunStatus.CANCELLED
    [question] = invocations
    _assert_question_failed_by_cancellation(question)


async def test_the_surface_writer_ends_a_run_waiting_for_the_user(tmp_path: Path) -> None:
    async with build(
        settings=_settings(tmp_path), script=_ask_user(), clock=FixedClock(NOW)
    ) as app:
        run_id = await app.runs.submit("Choose a region.")
        writer = SurfaceRunStateWriter(FixedClock(NOW), SequenceIdFactory())
        async with app.uow_factory() as uow:
            waiting = await uow.runs.get(run_id, app.principal)
            stopped = await writer.stop(uow, waiting, app.principal.principal_id)
        invocations, events = await _state(app, run_id)

    assert stopped.status is RunStatus.CANCELLED
    [question] = invocations
    _assert_question_failed_by_cancellation(question)
    cancellations = [event for event in events if event.event_type == "run.cancelled"]
    assert [(event.actor_type, event.actor_id) for event in cancellations] == [
        ("surface", app.principal.principal_id)
    ]


async def test_the_surface_writer_ends_a_run_waiting_for_approval_and_its_approval(
    tmp_path: Path,
) -> None:
    async with build(
        settings=_settings(tmp_path), script=_external_write(), clock=FixedClock(NOW)
    ) as app:
        run_id = await app.runs.submit("request an external write")
        [approval] = await app.approvals.list_pending(run_id=run_id)
        writer = SurfaceRunStateWriter(FixedClock(NOW), SequenceIdFactory())
        async with app.uow_factory() as uow:
            waiting = await uow.runs.get(run_id, app.principal)
            assert waiting.status is RunStatus.WAITING_FOR_APPROVAL
            stopped = await writer.stop(uow, waiting, app.principal.principal_id)
        pending = await app.approvals.list_pending(run_id=run_id)
        reaped = await app.approvals.get(approval.id)

    assert stopped.status is RunStatus.CANCELLED
    assert pending == []
    assert reaped.status is not ApprovalStatus.PENDING


async def test_the_surface_writer_only_requests_cancellation_of_a_running_run(
    tmp_path: Path,
) -> None:
    async with build(
        settings=_settings(tmp_path), script=_ask_user(), clock=FixedClock(NOW)
    ) as app:
        run_id = await app.runs.submit("Choose a region.")
        async with app.uow_factory() as uow:
            # Stand the parked run up as if a worker had claimed it again.
            await uow.runs.transition(run_id, RunStatus.WAITING_FOR_USER, RunStatus.QUEUED)
            running = await uow.runs.transition(run_id, RunStatus.QUEUED, RunStatus.RUNNING)
            requested = await SurfaceRunStateWriter(FixedClock(NOW), SequenceIdFactory()).stop(
                uow, running, app.principal.principal_id
            )
        _invocations, events = await _state(app, run_id)

    assert requested.status is RunStatus.RUNNING
    assert requested.cancel_requested_at == NOW
    assert not [event for event in events if event.event_type == "run.cancelled"]
