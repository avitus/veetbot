"""Preparation is a bounded runtime phase before automatic or deliberate recall."""

import json
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


@pytest.mark.parametrize("pin_spec", [False, True])
async def test_preparation_honors_pinned_legacy_search_filters(pin_spec: bool) -> None:
    from agent_core.domain.memory import BeliefType
    from agent_core.domain.messages import ToolCallItem
    from agent_core.memory.recall_preparation import SemanticRecallPreparation
    from agent_core.memory.retrieval import DeterministicQueryFormer

    async with build(
        settings=_settings(),
        script=FakeModelScript(turns=[ScriptedTurn(text="Done")]),
        clock=FixedClock(NOW),
    ) as app:
        run_id = await app.runs.submit("Find remembered constraints")
        run = await app.runs.wait_terminal(run_id)
        async with app.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(run_id)
        assert checkpoint is not None
        legacy = app.tool_pipeline._registry.get("memory.search", "1.1.0")
        arguments = {
            "text": "Remembered constraints",
            "scope": "general",
            "belief_types": ["preference"],
        }
        checkpoint.pinned_tool_versions["memory.search"] = "1.1.0"
        if pin_spec:
            checkpoint.pinned_tool_specs["memory.search"] = legacy.spec
        else:
            checkpoint.pinned_tool_specs.pop("memory.search", None)
        checkpoint.pending_tool_calls = [
            ToolCallItem(
                name="memory.search",
                call_id="legacy",
                item_index=0,
                arguments=arguments,
                raw_arguments=json.dumps(arguments),
            ).model_dump(mode="json")
        ]
        preparation = SemanticRecallPreparation(
            app.uow_factory,
            app.principal,
            app.memory_retriever,
            DeterministicQueryFormer(app.principal),
        )
        queries = await preparation.plan(run, checkpoint)
        assert len(queries) == 1, "Pinned legacy searches must receive an execution allowance"
        assert queries[0].belief_types == [BeliefType.PREFERENCE]
        assert queries[0].structured_belief_types == []


@pytest.mark.parametrize("session_scope", ["general", "workshop"])
@pytest.mark.parametrize("explicit_scope", [None, "another-project"])
async def test_deliberate_search_defaults_to_trusted_session_scope(
    monkeypatch: pytest.MonkeyPatch,
    session_scope: str,
    explicit_scope: str | None,
) -> None:
    from agent_core.memory.recall_preparation import SemanticRecallPreparation
    from agent_core.memory.retrieval import DeterministicQueryFormer

    arguments = {"text": "Remembered project constraints"}
    if explicit_scope is not None:
        arguments["scope"] = explicit_scope
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="memory.search",
                        call_id="search",
                        arguments=arguments,
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Done"),
        ]
    )
    async with build(settings=_settings(), script=script, clock=FixedClock(NOW)) as app:
        session = await app.services.sessions.create(
            app.principal, "general", {"project_scope": session_scope}
        )
        actual: list[RecallQuery] = []
        planned: list[RecallQuery] = []
        original = app.memory_retriever._recall

        async def observe(query: RecallQuery, **kwargs: Any) -> RecallResult:
            if kwargs["moment"] == "in_turn" and query.text == arguments["text"]:
                actual.append(query)
            return await original(query, **kwargs)

        class Preparation(SemanticRecallPreparation):
            async def plan(self, run: Run, checkpoint: RunCheckpoint) -> list[RecallQuery]:
                queries = await super().plan(run, checkpoint)
                if checkpoint.pending_tool_calls:
                    planned.extend(queries)
                return queries

        monkeypatch.setattr(app.memory_retriever, "_recall", observe)
        app.executor._prepare_recall = Preparation(
            app.uow_factory,
            app.principal,
            app.memory_retriever,
            DeterministicQueryFormer(app.principal),
        )
        run_id = await app.runs.submit("Find remembered project constraints", session.id)
        outcome = await app.runs.wait_terminal(run_id)
        assert outcome.status is RunStatus.COMPLETED, outcome.failure
        assert len(actual) == 1, "The model must not have to invent a project scope to search"
        assert actual[0].current_scope == (explicit_scope or session_scope)
        assert planned == actual, "Preparation and execution must share the resolved scope"


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
        # ADR-0281: the automatic round is reserved once and refreshed on every
        # build step; deliberate searches share the two slots it leaves.
        deliberate = [query for query in actual if (query.text or "").startswith("constraint")]
        automatic = [query for query in actual if query not in deliberate]
        assert len(deliberate) == 2, "Deliberate searches must stop at the reserved slots"
        # Base and delta on each of the five build steps.
        assert len(automatic) == 10, "Automatic recall refreshes on every build step"
        assert saved.recall_work.searches == 4 and saved.recall_work.rounds == 2
        refusals = [
            item
            for item in saved.conversation
            if isinstance(item, ToolResultItem) and item.is_error
        ]
        assert len(refusals) == 2
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
        assert calls == [(1, False), (1, True), (2, False)], (
            "The automatic round, the deliberate batch and the step-two refresh "
            "each save their exact task before provider work"
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
        # The automatic round reserves one slot and no round for this probe;
        # the three-search batch fills the allowance; the one-search batch is
        # refused; the two later build steps refresh without reserving.
        assert calls == ([(1, 0)] if expired else [(1, 0), (4, 1), (4, 1), (4, 1)]), (
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


def _bound_preparation(app: Any, clock: FixedClock, *, advance_seconds: float) -> Any:
    """An execution-bound preparation whose first provider call takes `advance_seconds`."""
    from datetime import timedelta

    class SlowPreparation:
        execution_budget = True
        prepared = 0

        async def plan(self, run: Run, checkpoint: RunCheckpoint) -> list[RecallQuery]:
            return [
                RecallQuery(
                    tenant_id=app.principal.tenant_id,
                    principal_id=app.principal.principal_id,
                    current_scope="general",
                    text="breakfast constraints",
                    subjects=[],
                    budget_tokens=2000,
                    max_items=20,
                    min_score=0.1,
                )
            ]

        async def prepare(self, run: Run, queries: Sequence[RecallQuery]) -> None:
            self.prepared += 1
            if self.prepared == 1:
                clock.advance(timedelta(seconds=advance_seconds))

    return SlowPreparation()


def _search_then_done() -> FakeModelScript:
    return FakeModelScript(
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


async def test_slow_provider_preparation_does_not_spend_the_search_deadline() -> None:
    """Preparation has its own clock: an eleven-second provider wait still leaves recall."""
    from agent_core.domain.messages import ToolResultItem

    clock = FixedClock(NOW)
    async with build(settings=_settings(), script=_search_then_done(), clock=clock) as app:
        app.executor._prepare_recall = _bound_preparation(app, clock, advance_seconds=11)
        run_id = await app.runs.submit("Suggest breakfast options")
        outcome = await app.runs.wait_terminal(run_id)
        assert outcome.status is RunStatus.COMPLETED, outcome.failure
        async with app.uow_factory() as uow:
            saved = await uow.checkpoints.latest(run_id)
            events = await uow.events.list_after(
                outcome.session_id, 0, app.principal, run_id=run_id
            )
        assert saved is not None
        refusals = [
            item
            for item in saved.conversation
            if isinstance(item, ToolResultItem) and item.is_error
        ]
        assert refusals == [], "A slow preparation must not refuse the turn's searches"
        recalled = [event for event in events if event.event_type == "memory.recalled"]
        assert recalled, "Automatic recall must still run after a slow preparation"
        assert not [event for event in events if event.event_type == "memory.recall.limited"]


async def test_refused_automatic_recall_leaves_an_event() -> None:
    """A recall refused past the outer bound is recorded, not silently dropped."""
    clock = FixedClock(NOW)
    async with build(settings=_settings(), script=_search_then_done(), clock=clock) as app:
        app.executor._prepare_recall = _bound_preparation(app, clock, advance_seconds=20)
        run_id = await app.runs.submit("Suggest breakfast options")
        outcome = await app.runs.wait_terminal(run_id)
        assert outcome.status is RunStatus.COMPLETED, outcome.failure
        async with app.uow_factory() as uow:
            events = await uow.events.list_after(
                outcome.session_id, 0, app.principal, run_id=run_id
            )
        limited = [event for event in events if event.event_type == "memory.recall.limited"]
        assert limited, "A refused automatic recall must leave a memory.recall.limited event"
        assert limited[0].payload["reason"] == "exhausted"
        assert limited[0].payload["step_number"] == 1
