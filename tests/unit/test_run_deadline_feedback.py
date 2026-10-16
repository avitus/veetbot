"""Deadline exhaustion must answer or fail visibly, including approval resumption."""

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.bootstrap import build
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.messages import (
    FakeModelScript,
    ModelAttempt,
    ModelEvent,
    ModelRequest,
    ResolvedModel,
    ScriptedToolCall,
    ScriptedTurn,
)
from agent_core.domain.runs import FailureReason, RunLimits, RunStatus
from tests.contract.support import NOW
from tests.gates.test_runtime_tool_budget import _settings, _tool_turn


def script() -> FakeModelScript:
    return FakeModelScript(
        turns=[
            _tool_turn(
                ScriptedToolCall(
                    name="demo.external_write",
                    arguments={"destination": "demo", "content": "hello"},
                )
            ),
            ScriptedTurn(text="Done."),
        ]
    )


async def test_approval_cannot_outlive_the_run_deadline() -> None:
    clock = FixedClock(NOW)
    deadline = NOW + timedelta(minutes=5)
    async with build(
        settings=_settings(), script=script(), clock=clock, limits=RunLimits(deadline_at=deadline)
    ) as app:
        run_id = await app.runs.submit("Perform the action.")
        [approval] = await app.approvals.list_pending(run_id=run_id)
        assert approval.expires_at == deadline


async def test_approval_resuming_after_deadline_fails_visibly() -> None:
    clock = FixedClock(NOW)
    async with build(
        settings=_settings(),
        script=script(),
        clock=clock,
        limits=RunLimits(deadline_at=NOW + timedelta(minutes=5)),
    ) as app:
        run_id = await app.runs.submit("Perform the action.")
        [approval] = await app.approvals.list_pending(run_id=run_id)
        clock.advance(timedelta(minutes=6))
        await app.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        result = await app.runs.get(run_id)
        assert result.status is RunStatus.FAILED
        assert result.failure is not None
        assert result.failure.reason is FailureReason.DEADLINE_EXCEEDED
        assert result.failure.message
        assert result.tool_call_count == 0


async def test_deadline_reserves_time_for_the_final_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FixedClock(NOW)
    original = FakeModelProvider.stream

    async def slow_research(
        provider: FakeModelProvider,
        request: ModelRequest,
        resolved: ResolvedModel,
        attempt: ModelAttempt,
    ) -> AsyncIterator[ModelEvent]:
        async for event in original(provider, request, resolved, attempt):
            yield event
        if len(provider.requests) == 1:
            clock.advance(timedelta(minutes=4))

    monkeypatch.setattr(FakeModelProvider, "stream", slow_research)
    research = FakeModelScript(
        turns=[
            _tool_turn(ScriptedToolCall(name="math.calculate", arguments={"expression": "2 + 2"})),
            ScriptedTurn(text="Here is the evidence so far.", context_contains="time budget"),
        ]
    )
    async with build(
        settings=_settings(),
        script=research,
        clock=clock,
        limits=RunLimits(deadline_at=NOW + timedelta(minutes=5), synthesis_reserve_steps=1),
    ) as app:
        run_id = await app.runs.submit("Research and report.")
        result = await app.runs.get(run_id)
        assert result.status is RunStatus.COMPLETED, result.failure
        assert result.final_message == "Here is the evidence so far."
        assert result.tool_call_count == 0
