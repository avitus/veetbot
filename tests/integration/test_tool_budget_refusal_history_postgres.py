"""A tool-call budget refusal survives the event log (PostgreSQL).

Found on 2026-10-08 while repairing lost-checkpoint resume (ADR-0166): the
loop answered a call past ``max_tool_calls`` with a ``tool.budget_exhausted``
result in the in-memory conversation only. PostgreSQL checkpoints store the
conversation as a session-history reference, and session history is built
from events, so every later reading of that history held the refused call with
no result, which context assembly rejects as an orphaned tool call. The
in-memory gate stores conversations inline and could not see it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, select

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.adapters.persistence.database import create_engine
from agent_core.adapters.persistence.sqlalchemy_models import CheckpointRow, ToolInvocationRow
from agent_core.bootstrap import Composition, build
from agent_core.context.history import validate_tool_pairs
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextPart,
    ToolResultItem,
    UserMessage,
)
from agent_core.domain.runs import RunLimits, RunStatus
from agent_core.domain.tools import ToolOutcome
from agent_core.runtime import loop as runtime_loop
from agent_core.runtime.worker import DurableWorker, MaintenanceWorker
from tests.contract.support import NOW
from tests.integration.m2_support import database_settings

FITTED = "budget-fitted"
REFUSED = "budget-refused"
LIMITS = RunLimits(max_steps=4, max_model_calls=4, max_tool_calls=1)


class _InjectedWorkerCrash(BaseException):
    pass


def _script(*answers: str) -> FakeModelScript:
    """One batch of two calls against a one-call budget, then plain answers."""

    return FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="math.calculate", arguments={"expression": "1+1"}, call_id=FITTED
                    ),
                    ScriptedToolCall(
                        name="math.calculate", arguments={"expression": "2+2"}, call_id=REFUSED
                    ),
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            *(ScriptedTurn(text=answer) for answer in answers),
        ]
    )


async def _run_worker(composition: Composition, clock: FixedClock, worker_id: str) -> None:
    worker = DurableWorker(
        uow_factory=composition.uow_factory,
        executor=composition.executor,
        clock=clock,
        worker_id=worker_id,
    )
    assert await worker.run_once()


def _user_texts(items: list[Any]) -> list[str]:
    return [
        part.text
        for item in items
        if isinstance(item, UserMessage)
        for part in item.content
        if isinstance(part, TextPart)
    ]


def _reason_codes(items: list[Any], call_id: str) -> list[str]:
    return [
        ToolOutcome.model_validate_json(part.text).reason_code
        for item in items
        if isinstance(item, ToolResultItem) and item.call_id == call_id
        for part in item.content
        if isinstance(part, TextPart)
    ]


async def test_a_budget_refusal_leaves_the_session_open_to_the_next_message() -> None:
    clock = FixedClock(NOW)
    async with build(
        settings=database_settings(),
        storage="postgres",
        script=_script("first answer", "second answer"),
        clock=clock,
        limits=LIMITS,
    ) as composition:
        first_id = await composition.runs.submit("look two things up")
        await _run_worker(composition, clock, "budget-first")
        first = await composition.runs.get(first_id)
        assert first.status is RunStatus.COMPLETED, first.failure

        second_id = await composition.runs.submit("then answer this", first.session_id)
        await _run_worker(composition, clock, "budget-second")
        second = await composition.runs.get(second_id)
        async with composition.uow_factory() as uow:
            history = await uow.history.catch_up(first.session_id)
        provider = composition.executor._model_provider
        assert isinstance(provider, FakeModelProvider)
        follow_up = provider.requests[-1]

    assert second.failure is None
    assert second.status is RunStatus.COMPLETED
    assert second.final_message == "second answer"
    # An orphaned pair makes history selection cut the whole first turn, the
    # owner's message included, out of the follow-up's verbatim context and
    # leave only a summary of it.
    texts = _user_texts(follow_up.conversation)
    assert not any(text.startswith("Structured context summary") for text in texts)
    assert any("look two things up" in text for text in texts)
    assert _reason_codes(follow_up.conversation, REFUSED) == ["tool.budget_exhausted"]
    validate_tool_pairs(history.items)
    assert _reason_codes(history.items, REFUSED) == ["tool.budget_exhausted"]
    # The refusal is not tool usage: only the call that fitted was counted.
    assert first.tool_call_count == 1
    assert first.usage.tool_calls == 1


async def test_a_resume_from_the_tool_pending_checkpoint_keeps_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FixedClock(NOW)
    original_checkpoint = runtime_loop.checkpoint
    crashed = False

    async def crash_after_tool_pending(context: Any, trigger: str) -> None:
        nonlocal crashed
        await original_checkpoint(context, trigger)
        if trigger == "tool_pending" and not crashed:
            crashed = True
            raise _InjectedWorkerCrash

    async with build(
        settings=database_settings(),
        storage="postgres",
        script=_script("answered after resume"),
        clock=clock,
        limits=LIMITS,
    ) as composition:
        run_id = await composition.runs.submit("look two things up")
        monkeypatch.setattr(runtime_loop, "checkpoint", crash_after_tool_pending)
        with pytest.raises(_InjectedWorkerCrash):
            await _run_worker(composition, clock, "budget-pending-crash")
        monkeypatch.setattr(runtime_loop, "checkpoint", original_checkpoint)
        async with composition.uow_factory() as uow:
            pending = await uow.checkpoints.latest(run_id)
        assert pending is not None
        assert [call["call_id"] for call in pending.pending_tool_calls] == [FITTED]

        clock.advance(timedelta(seconds=31))
        maintenance = MaintenanceWorker(
            uow_factory=composition.uow_factory,
            clock=clock,
            poll_interval_seconds=0,
        )
        assert await maintenance.run_once() == 1
        clock.advance(timedelta(seconds=2))
        await _run_worker(composition, clock, "budget-pending-recovery")
        recovered = await composition.runs.get(run_id)
        async with composition.uow_factory() as uow:
            history = await uow.history.catch_up(recovered.session_id)

    assert recovered.failure is None
    assert recovered.status is RunStatus.COMPLETED
    assert recovered.final_message == "answered after resume"
    assert recovered.tool_call_count == 1
    validate_tool_pairs(history.items)
    # The materialized tool_pending checkpoint already held the refusal, so
    # the resume re-dispatched only the call that fitted.
    assert _reason_codes(pending.conversation, REFUSED) == ["tool.budget_exhausted"]


@pytest.mark.parametrize(
    ("interrupted_at", "lost"),
    [
        # The refusal is in the log; the batch is pending.
        ("after_tool_pending", 1),  # restore the model_response checkpoint
        ("after_tool_pending", 2),  # restore the submission seed
        ("after_tool_pending", 3),  # no checkpoint survives
        # The model's turn is in the log; its refusal is not.
        ("before_refusals", 0),  # restore the model_response checkpoint
        ("before_refusals", 1),  # restore the submission seed
        ("before_refusals", 2),  # no checkpoint survives
    ],
)
async def test_a_resume_behind_lost_checkpoints_never_runs_a_refused_call(
    monkeypatch: pytest.MonkeyPatch,
    interrupted_at: str,
    lost: int,
) -> None:
    """A batch rebuilt from history is fitted to the budget it was proposed under.

    Found on 2026-10-08: a resume that rebuilt the batch from the conversation,
    because the checkpoint that recorded it as pending was lost, dispatched
    every unanswered call of the turn. The refused call ran, and recording two
    calls against a one-call budget failed the run with BUDGET_EXCEEDED.
    """

    settings = database_settings()
    engine = create_engine(settings.database_url)
    clock = FixedClock(NOW)
    original_checkpoint = runtime_loop.checkpoint
    original_record_refusals = runtime_loop._record_refusals

    async def crash_after_tool_pending(context: Any, trigger: str) -> None:
        await original_checkpoint(context, trigger)
        if trigger == "tool_pending":
            raise _InjectedWorkerCrash

    async def crash_before_refusals(context: Any, refused: Any) -> None:
        raise _InjectedWorkerCrash

    try:
        async with build(
            settings=settings,
            storage="postgres",
            script=_script("answered after resume"),
            clock=clock,
            limits=LIMITS,
        ) as composition:
            run_id = await composition.runs.submit("look two things up")
            if interrupted_at == "after_tool_pending":
                monkeypatch.setattr(runtime_loop, "checkpoint", crash_after_tool_pending)
            else:
                monkeypatch.setattr(runtime_loop, "_record_refusals", crash_before_refusals)
            with pytest.raises(_InjectedWorkerCrash):
                await _run_worker(composition, clock, "budget-interrupted")
            monkeypatch.setattr(runtime_loop, "checkpoint", original_checkpoint)
            monkeypatch.setattr(runtime_loop, "_record_refusals", original_record_refusals)

            async with engine.begin() as connection:
                newest = await connection.scalar(
                    select(func.max(CheckpointRow.version)).where(CheckpointRow.run_id == run_id)
                )
                assert newest is not None and newest >= lost
                await connection.execute(
                    delete(CheckpointRow).where(
                        CheckpointRow.run_id == run_id,
                        CheckpointRow.version > newest - lost,
                    )
                )

            clock.advance(timedelta(seconds=31))
            maintenance = MaintenanceWorker(
                uow_factory=composition.uow_factory,
                clock=clock,
                poll_interval_seconds=0,
            )
            assert await maintenance.run_once() == 1
            clock.advance(timedelta(seconds=2))
            await _run_worker(composition, clock, "budget-resumed")
            recovered = await composition.runs.get(run_id)
            async with composition.uow_factory() as uow:
                history = await uow.history.catch_up(recovered.session_id)
            async with engine.connect() as connection:
                executions = dict(
                    (
                        await connection.execute(
                            select(ToolInvocationRow.provider_call_id, func.count())
                            .where(ToolInvocationRow.run_id == run_id)
                            .group_by(ToolInvocationRow.provider_call_id)
                        )
                    )
                    .tuples()
                    .all()
                )
    finally:
        await engine.dispose()

    assert recovered.failure is None
    assert recovered.status is RunStatus.COMPLETED
    assert recovered.final_message == "answered after resume"
    # The refused call never reached the tool executor, and only the call
    # that fitted counts as usage.
    assert executions == {FITTED: 1}
    assert recovered.tool_call_count == 1
    assert recovered.usage.tool_calls == 1
    validate_tool_pairs(history.items)
    assert _reason_codes(history.items, REFUSED) == ["tool.budget_exhausted"]
