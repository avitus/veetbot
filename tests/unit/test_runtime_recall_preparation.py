"""Preparation is a bounded runtime phase before automatic or deliberate recall."""

from collections.abc import Sequence
from typing import Any

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import build
from agent_core.domain.memory import RecallQuery, RecallResult
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.runs import Run, RunCheckpoint, RunStatus
from tests.contract.support import NOW
from tests.gates.test_runtime_tool_budget import _settings


async def test_reserved_allowance_bounds_actual_sparse_searches_across_all_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core.domain.messages import ToolResultItem
    from agent_core.memory.recall_preparation import SemanticRecallPreparation
    from agent_core.memory.retrieval import DeterministicQueryFormer

    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="memory.search",
                        call_id=f"bounded-{index}",
                        arguments={"text": f"constraint {index}", "scope": "general"},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            )
            for index in range(4)
        ]
        + [ScriptedTurn(text="Done")]
    )
    async with build(settings=_settings(), script=script, clock=FixedClock(NOW)) as app:
        actual = []
        original = app.memory_retriever._recall

        async def observe(query: RecallQuery, **kwargs: Any) -> RecallResult:
            if kwargs["moment"] == "in_turn":
                actual.append(query)
            return await original(query, **kwargs)

        monkeypatch.setattr(app.memory_retriever, "_recall", observe)
        app.executor._prepare_recall = SemanticRecallPreparation(
            app.uow_factory,
            app.principal,
            app.memory_retriever,
            DeterministicQueryFormer(app.principal),
        )
        run_id = await app.runs.submit("Recall my constraints")
        outcome = await app.runs.wait_terminal(run_id)
        assert outcome.status is RunStatus.COMPLETED
        async with app.uow_factory() as uow:
            saved = await uow.checkpoints.latest(run_id)
        assert saved is not None and saved.recall_work is not None
        assert len(actual) <= 4, "Optional preparation must not leave unlimited sparse execution"
        refusals = [
            item
            for item in saved.conversation
            if isinstance(item, ToolResultItem) and item.is_error
        ]
        assert len(refusals) == 3
        assert all("memory_recall_budget_exhausted" in item.model_dump_json() for item in refusals)


@pytest.mark.parametrize("failure", [False, True])
async def test_preparation_reads_durable_task_before_model_and_tool_execution(
    failure: bool,
) -> None:
    calls: list[tuple[int, bool]] = []
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="memory.search",
                        call_id="search",
                        arguments={"text": "breakfast constraints", "scope": "general"},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Done"),
        ]
    )
    async with build(settings=_settings(), script=script, clock=FixedClock(NOW)) as app:
        from agent_core.domain.memory import RecallQuery

        async def prepare(run: Run, checkpoint: RunCheckpoint) -> None:
            assert not app.uow_factory.is_open()
            async with app.uow_factory() as uow:
                saved = await uow.checkpoints.latest(run.id)
            assert saved == checkpoint, "Preparation must bind the exact persisted task"
            assert saved is not None and saved.budget_state.get("context_reader_sha256")
            calls.append((run.step_count, bool(checkpoint.pending_tool_calls)))
            if failure:
                raise ValueError("Synthetic unavailable preparation")

        class Preparation:
            pending: RunCheckpoint

            async def plan(self, run: Run, checkpoint: RunCheckpoint) -> list[RecallQuery]:
                self.pending = checkpoint
                return [
                    RecallQuery(
                        tenant_id=app.principal.tenant_id,
                        principal_id=app.principal.principal_id,
                        current_scope="general",
                        text="breakfast",
                        subjects=[],
                        budget_tokens=2000,
                        max_items=20,
                        min_score=0.1,
                    )
                ]

            async def prepare(self, run: Run, queries: Sequence[RecallQuery]) -> None:
                await prepare(run, self.pending)

        app.executor._prepare_recall = Preparation()
        run_id = await app.runs.submit("Suggest breakfast options")
        outcome = await app.runs.wait_terminal(run_id)
        assert outcome.status is RunStatus.COMPLETED, outcome.failure
        assert calls == [(1, False), (1, True)], (
            "The two admitted rounds save their exact task before provider work"
        )


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("expired", [False, True])
async def test_optional_preparation_budget_is_shared_and_durable(
    failure: bool, expired: bool
) -> None:
    from datetime import timedelta

    from agent_core.domain.memory import RecallQuery

    calls: list[tuple[int, int]] = []
    clock = FixedClock(NOW)
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="memory.search",
                        call_id=f"search-{batch}-{index}",
                        arguments={"text": f"constraint {batch}-{index}", "scope": "general"},
                    )
                    for index in range(size)
                ],
                stop_reason=StopReason.TOOL_USE,
            )
            for batch, size in enumerate((3, 1))
        ]
        + [ScriptedTurn(text="Done")]
    )
    async with build(settings=_settings(), script=script, clock=clock) as app:

        class Probe:
            async def plan(self, run: Run, checkpoint: RunCheckpoint) -> list[RecallQuery]:
                return [
                    RecallQuery(
                        tenant_id=app.principal.tenant_id,
                        principal_id=app.principal.principal_id,
                        current_scope="general",
                        text=f"query {index}",
                        subjects=[],
                        budget_tokens=2000,
                        max_items=20,
                        min_score=0.1,
                    )
                    for index in range(len(checkpoint.pending_tool_calls) or 1)
                ]

            async def prepare(self, run: Run, queries: Sequence[RecallQuery]) -> None:
                assert not app.uow_factory.is_open()
                async with app.uow_factory() as uow:
                    saved = await uow.checkpoints.latest(run.id)
                assert saved is not None
                budget = getattr(saved, "recall_work", None)
                calls.append((-1, -1) if budget is None else (budget.searches, budget.rounds))
                if expired:
                    clock.advance(timedelta(seconds=11))
                if failure:
                    raise ValueError("Synthetic optional provider failure")

        app.executor._prepare_recall = Probe()
        run_id = await app.runs.submit("Suggest breakfast options")
        outcome = await app.runs.wait_terminal(run_id)
        assert outcome.status is RunStatus.COMPLETED, outcome.failure
        assert calls == ([(1, 1)] if expired else [(1, 1), (4, 2)]), (
            "Each new context or tool batch must share the persisted run allowance"
        )
        async with app.uow_factory() as uow:
            saved = await uow.checkpoints.latest(run_id)
            assert saved is not None and saved.recall_work is not None
            assert saved.recall_work.searches == calls[-1][0]
            events = await uow.events.list_after(
                outcome.session_id, 0, app.principal, run_id=run_id
            )
        checkpoints = [event for event in events if event.event_type == "run.checkpointed"]
        assert checkpoints[-1].payload["recall_work"] == saved.recall_work.model_dump(mode="json")


async def test_checkpoint_loss_recovers_budget_after_the_history_boundary() -> None:
    from datetime import timedelta
    from uuid import UUID

    from agent_core.domain.events import NewEvent
    from agent_core.domain.runs import RecallWorkBudget
    from agent_core.runtime.checkpoints import DurableCheckpointSeeder
    from tests.contract.support import RUN_ID, SESSION_ID, principal, run
    from tests.contract.test_runtime_semantic_queries_contract import runtime_stack

    clock, factory, _, _, _, _ = await runtime_stack()
    budget = RecallWorkBudget(searches=4, rounds=2, deadline_at=NOW + timedelta(seconds=10))
    async with factory() as uow:
        source = await uow.events.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=RUN_ID,
                event_type="user.message.created",
                actor_type="principal",
                actor_id=principal().principal_id,
                payload={"content": "Suggest breakfast"},
            )
        )
        await uow.events.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=RUN_ID,
                event_type="run.checkpointed",
                actor_type="runtime",
                payload={
                    "version": 2,
                    "trigger": "memory_preparation",
                    "recall_work": budget.model_dump(mode="json"),
                },
            )
        )
        other = run(status=RunStatus.COMPLETED).model_copy(
            update={"id": UUID(int=999), "finished_at": NOW}
        )
        await uow.runs.create(other)
        await uow.events.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=other.id,
                event_type="run.checkpointed",
                actor_type="runtime",
                payload={"version": 1, "trigger": "seed", "recall_work": None},
            )
        )
        await uow.checkpoints.delete_nonterminal(RUN_ID)
        live = await uow.runs.get(RUN_ID, principal())
        recovered = await DurableCheckpointSeeder(clock)(
            uow, live, source.sequence, None, principal()
        )
        assert recovered.recall_work == budget, (
            "Checkpoint recovery must retain spent work beyond the conversation cutoff"
        )
        # A second loss must not reset the recovered content-free reservation.
        await uow.checkpoints.delete_nonterminal(RUN_ID)
        again = await DurableCheckpointSeeder(clock)(uow, live, source.sequence, None, principal())
        assert again.recall_work == budget
