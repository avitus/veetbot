"""An unchanged checkpoint must not freeze mutable recall evidence."""

from datetime import timedelta

import pytest

from agent_core.domain.runs import Run, RunCheckpoint
from agent_core.memory.retrieval import DeterministicQueryFormer
from tests.contract.memory_fixtures import memory
from tests.contract.semantic_memory_cases import SPACE, VECTOR
from tests.contract.support import NOW, RUN_ID, SESSION_ID, agent, principal
from tests.contract.test_runtime_semantic_queries_contract import query, runtime_stack
from tests.gates.test_context_m7 import _builder


async def test_prepared_query_reaches_next_context_without_changing_prefix() -> None:
    _, factory, provider, _, retriever, _ = await runtime_stack()
    original = memory(statement="Dairy causes the user digestive discomfort").model_copy(
        update={"subject": "diet"}
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(original)
        source = await uow.semantic_memory.capture(principal(), original.id, SPACE)
        assert source is not None and await uow.semantic_memory.put(principal(), source, VECTOR)
        active_run = await uow.runs.get(RUN_ID, principal())
        checkpoint = await uow.checkpoints.latest(RUN_ID)
    assert checkpoint is not None
    former = DeterministicQueryFormer(principal(), current_scope="project-a")
    builder = _builder(memory_retriever=retriever, query_former=former)
    before = await builder.build(active_run, checkpoint, agent(), principal())
    assert original.statement not in before.model_dump_json()
    assert provider.calls == []
    assert (
        await retriever.prepare_semantic_queries([query()], session_id=SESSION_ID, run_id=RUN_ID)
        == 1
    )
    after = await builder.build(active_run, checkpoint, agent(), principal())
    assert original.statement in after.model_dump_json(), "completed sparse recall hid a new match"
    assert before.metadata["prefix_sha256"] == after.metadata["prefix_sha256"]
    assert len(provider.calls) == 1


@pytest.mark.parametrize("change", ["source", "erasure", "expiry", "admission", "lease"])
async def test_completed_context_cannot_replay_invalidated_semantic_evidence(change: str) -> None:
    clock, factory, provider, _, retriever, state = await runtime_stack(
        run_changes={"lease_expires_at": NOW + timedelta(seconds=30)} if change == "lease" else None
    )
    original = memory(statement="Dairy causes the user digestive discomfort").model_copy(
        update={
            "subject": "diet",
            "expires_at": clock.now() + timedelta(minutes=1) if change == "expiry" else None,
        }
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(original)
        source = await uow.semantic_memory.capture(principal(), original.id, SPACE)
        assert source is not None and await uow.semantic_memory.put(principal(), source, VECTOR)
        active_run: Run = await uow.runs.get(RUN_ID, principal())
        saved = await uow.checkpoints.latest(RUN_ID)
    assert saved is not None
    checkpoint: RunCheckpoint = saved
    builder = _builder(
        memory_retriever=retriever,
        query_former=DeterministicQueryFormer(principal(), current_scope="project-a"),
    )
    assert (
        await retriever.prepare_semantic_queries([query()], session_id=SESSION_ID, run_id=RUN_ID)
        == 1
    )
    before = await builder.build(active_run, checkpoint, agent(), principal())
    assert original.statement in before.model_dump_json()
    if change == "source":
        async with factory() as uow:
            await uow.memories.reinforce(
                original.model_copy(update={"statement": "The dietary constraint was corrected"})
            )
    elif change == "erasure":
        async with factory() as uow:
            await uow.memories.fence_for_erasure(principal(), [original.id])
    elif change == "expiry":
        clock.advance(timedelta(minutes=1))
    elif change == "admission":
        state.enabled = False
    else:
        clock.advance(timedelta(seconds=30))
    after = await builder.build(active_run, checkpoint, agent(), principal())
    assert original.statement not in after.model_dump_json(), f"cached recall survived {change}"
    assert before.metadata["prefix_sha256"] == after.metadata["prefix_sha256"]
    assert len(provider.calls) == 1


@pytest.mark.parametrize(
    ("message", "statement"),
    [
        ("Summarize atlas plans", "Atlas plans use a weekly checkpoint"),
        ("Recall answer style", "Answer style should be concise"),
        ("Recall Tuesday routine", "Tuesday routine includes an evening swim"),
    ],
)
async def test_sparse_recall_also_refreshes_new_evidence(message: str, statement: str) -> None:
    from agent_core.domain.messages import TextPart, UserMessage

    _, factory, provider, _, retriever, _ = await runtime_stack()
    async with factory() as uow:
        active_run = await uow.runs.get(RUN_ID, principal())
        checkpoint = await uow.checkpoints.latest(RUN_ID)
    assert checkpoint is not None
    checkpoint.conversation = [UserMessage(content=[TextPart(text=message)])]
    builder = _builder(
        memory_retriever=retriever,
        query_former=DeterministicQueryFormer(principal(), current_scope="project-a"),
    )
    before = await builder.assemble(active_run, checkpoint, agent(), principal())
    assert statement not in before.request.model_dump_json()
    async with factory() as uow:
        await uow.memories.upsert_belief(memory(statement=statement))
    after = await builder.assemble(active_run, checkpoint, agent(), principal())
    assert statement in after.request.model_dump_json()
    assert before.request.metadata["prefix_sha256"] == after.request.metadata["prefix_sha256"]
    assert builder._recall_tasks == {}, "production must not retain rendered evidence"
    assert provider.calls == []


@pytest.mark.parametrize("people_enabled", [False, True])
async def test_composed_context_refreshes_elapsed_expiry(people_enabled: bool) -> None:
    from dataclasses import replace

    from agent_core.adapters.determinism import FixedClock
    from agent_core.bootstrap import build
    from tests.integration.m2_support import memory_settings

    clock = FixedClock(NOW)
    async with build(
        settings=replace(memory_settings(), people_enabled=people_enabled),
        clock=clock,
        principal=principal(),
    ) as app:
        run_id = await app.runs.submit("Recall Tuesday routine")
        active = await app.runs.wait_terminal(run_id)
        original = memory(statement="Tuesday routine includes an evening swim").model_copy(
            update={"expires_at": NOW + timedelta(minutes=1)}
        )
        async with app.uow_factory() as uow:
            saved = await uow.checkpoints.latest(run_id)
            configured = await uow.agents.get_version(active.agent_id, active.agent_version)
            await uow.memories.upsert_belief(original)
        assert saved is not None
        before = await app.executor._context_builder.assemble(
            active, saved, configured, principal()
        )
        assert original.statement in before.request.model_dump_json()
        clock.advance(timedelta(minutes=1))
        after = await app.executor._context_builder.assemble(active, saved, configured, principal())
        assert original.statement not in after.request.model_dump_json()
        assert before.request.metadata["prefix_sha256"] == after.request.metadata["prefix_sha256"]


@pytest.mark.parametrize(
    "change", ["source", "erasure", "expiry", "admission", "lease", "checkpoint", "search_deadline"]
)
async def test_saved_search_result_cannot_hide_invalidated_evidence(change: str) -> None:
    from agent_core.domain.messages import TextPart, ToolCallItem, ToolResultItem
    from agent_core.domain.policies import TrustLevel
    from agent_core.tools.memory_search import MemorySearchTool
    from tests.contract.support import tool_context

    clock, factory, provider, _, retriever, state = await runtime_stack(
        run_changes={"lease_expires_at": NOW + timedelta(seconds=30)} if change == "lease" else None
    )
    original = memory(statement="Dairy causes the user digestive discomfort").model_copy(
        update={
            "subject": "diet",
            "expires_at": NOW + timedelta(minutes=1) if change == "expiry" else None,
        }
    )
    tool = MemorySearchTool(retriever)
    arguments = {"text": "Suggest a breakfast", "scope": "project-a"}
    search = tool.query(
        arguments, tenant_id=principal().tenant_id, principal_id=principal().principal_id
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(original)
        source = await uow.semantic_memory.capture(principal(), original.id, SPACE)
        assert source is not None and await uow.semantic_memory.put(principal(), source, VECTOR)
        active = await uow.runs.get(RUN_ID, principal())
        checkpoint = await uow.checkpoints.latest(RUN_ID)
    assert checkpoint is not None
    assert (
        await retriever.prepare_semantic_queries([search], session_id=SESSION_ID, run_id=RUN_ID)
        == 1
    )
    result = await tool.execute(arguments, tool_context())
    assert original.statement in result.model_dump_json()
    checkpoint.conversation.extend(
        [
            ToolCallItem(
                call_id="saved",
                item_index=0,
                name="memory.search",
                arguments=arguments,
                raw_arguments="{}",
            ),
            ToolResultItem(
                call_id="saved",
                content=result.content,
                trust=TrustLevel.MEMORY,
                memory_recall=result.memory_recall,
            ),
        ]
    )
    builder = _builder(
        memory_retriever=retriever,
        query_former=DeterministicQueryFormer(principal(), current_scope="project-a"),
    )
    before = await builder.build(active, checkpoint, agent(), principal())
    assert original.statement in before.model_dump_json()
    if change == "source":
        async with factory() as uow:
            await uow.memories.reinforce(
                original.model_copy(update={"statement": "The dietary constraint was corrected"})
            )
    elif change == "erasure":
        async with factory() as uow:
            await uow.memories.fence_for_erasure(principal(), [original.id])
    elif change == "expiry":
        clock.advance(timedelta(minutes=1))
    elif change == "admission":
        state.enabled = False
    elif change in {"checkpoint", "search_deadline"}:
        from agent_core.domain.runs import RecallWorkBudget

        if change == "search_deadline":
            checkpoint.recall_work = RecallWorkBudget(searches=4, rounds=2, deadline_at=NOW)
        async with factory() as uow:
            await uow.checkpoints.write(
                RUN_ID, checkpoint.model_copy(update={"version": checkpoint.version + 1}), full=True
            )
    else:
        clock.advance(timedelta(seconds=30))
    after = await builder.build(active, checkpoint, agent(), principal())
    if change in {"checkpoint", "search_deadline"}:
        assert original.statement in after.model_dump_json(), (
            "Current evidence survives a checkpoint advance"
        )
    else:
        assert original.statement not in after.model_dump_json(), f"saved search survived {change}"
    assert isinstance(result.content[0], TextPart)
    assert any(
        isinstance(item, ToolResultItem) and item.content == [result.content[0]]
        for item in checkpoint.conversation
    ), "Canonical history remains intact"
    assert len(provider.calls) == 1, "Validation must not issue another embedding call"
