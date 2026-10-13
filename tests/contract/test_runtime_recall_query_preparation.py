"""Automatic and deliberate preparation must use the exact later recall query."""

import json
from collections.abc import Sequence
from typing import Any

import pytest

from agent_core.context.working_state import WorkingStateManager
from agent_core.domain.context import WorkingState
from agent_core.domain.memory import RecallQuery
from agent_core.domain.messages import ToolCallItem
from agent_core.domain.sessions import project_scope
from agent_core.memory.recall_preparation import SemanticRecallPreparation
from agent_core.memory.retrieval import DeterministicQueryFormer
from agent_core.tools.memory_search import MemorySearchTool
from tests.contract.support import RUN_ID, SESSION_ID, agent, principal
from tests.contract.test_runtime_semantic_queries_contract import runtime_stack


@pytest.mark.parametrize("deliberate", [False, True])
@pytest.mark.parametrize("task_context", [False, True])
async def test_prepared_queries_match_the_governed_reader(
    deliberate: bool, task_context: bool
) -> None:
    _, factory, _, _, retriever, _ = await runtime_stack()
    async with factory() as uow:
        live = await uow.runs.get(RUN_ID, principal())
        session = await uow.sessions.get(SESSION_ID, principal())
        checkpoint = await uow.checkpoints.latest(RUN_ID)
        await uow.agents.put(agent().model_copy(update={"enabled_tools": ["memory.search"]}))
    assert checkpoint is not None
    WorkingStateManager.store(
        checkpoint.working_state,
        WorkingState(
            next_action="Choose breakfast for the trip", constraints=["Respect allergies"]
        ),
    )
    former = DeterministicQueryFormer(
        principal(), planning="task_context" if task_context else "baseline", admitted=lambda: True
    )
    tool = MemorySearchTool(retriever, model_planning=True)
    expected = former.form(
        live,
        WorkingStateManager.load(checkpoint.working_state),
        "Suggest breakfast options",
        current_scope=project_scope(session.metadata),
    )
    if deliberate:
        args: dict[str, Any] = {
            "text": "Which meal fits my constraints?",
            "scope": "general",
            "type_hints": ["preference"],
        }
        checkpoint.pending_tool_calls = [
            ToolCallItem(
                item_index=0,
                raw_arguments=json.dumps(args),
                call_id="search",
                name="memory.search",
                arguments=args,
            ).model_dump(mode="json")
        ]
        expected = [
            tool.query(args, tenant_id=principal().tenant_id, principal_id=principal().principal_id)
        ]
    calls: list[RecallQuery] = []

    async def capture(queries: Sequence[RecallQuery], **kwargs: Any) -> int:
        assert kwargs == {"session_id": SESSION_ID, "run_id": RUN_ID}
        assert not factory.is_open()
        calls.extend(queries)
        return len(queries)

    retriever.prepare_semantic_queries = capture  # type: ignore[method-assign]
    preparation = SemanticRecallPreparation(factory, principal(), retriever, former)
    await preparation.prepare(live, await preparation.plan(live, checkpoint))
    assert calls == expected, (
        "Preparation and recall must have identical scope, filters and budgets"
    )


async def test_invalid_or_overlarge_deliberate_batch_cannot_start_work() -> None:
    _, factory, provider, _, retriever, _ = await runtime_stack()
    async with factory() as uow:
        live = await uow.runs.get(RUN_ID, principal())
        checkpoint = await uow.checkpoints.latest(RUN_ID)
        await uow.agents.put(agent().model_copy(update={"enabled_tools": ["memory.search"]}))
    assert checkpoint is not None
    calls: list[RecallQuery] = []

    async def capture(queries: Sequence[RecallQuery], **_: Any) -> int:
        calls.extend(queries)
        return len(queries)

    retriever.prepare_semantic_queries = capture  # type: ignore[method-assign]
    preparation = SemanticRecallPreparation(
        factory, principal(), retriever, DeterministicQueryFormer(principal())
    )
    invalid = ToolCallItem(
        item_index=0,
        raw_arguments="{}",
        call_id="bad",
        name="memory.search",
        arguments={"text": "A query", "scope": "general", "extra": "not admitted"},
    )
    checkpoint.pending_tool_calls = [invalid.model_dump(mode="json")]
    await preparation.prepare(live, await preparation.plan(live, checkpoint))
    assert not calls and not provider.calls
    valid = invalid.model_copy(update={"arguments": {"text": "A query", "scope": "general"}})
    checkpoint.pending_tool_calls = [
        valid.model_copy(update={"call_id": f"search{i}"}).model_dump(mode="json") for i in range(5)
    ]
    await preparation.prepare(live, await preparation.plan(live, checkpoint))
    assert not calls, "More than four searches cannot become a hidden embedding batch"


@pytest.mark.parametrize("tokens,items", [(1900, 19), (2000, 20)])
async def test_automatic_preparation_uses_the_remaining_rendered_allowance(
    tokens: int, items: int
) -> None:
    from tests.unit.test_context_memory_budget import search_result

    _, factory, _, _, retriever, _ = await runtime_stack()
    async with factory() as uow:
        live = await uow.runs.get(RUN_ID, principal())
        checkpoint = await uow.checkpoints.latest(RUN_ID)
    assert checkpoint is not None
    checkpoint.conversation.extend(
        search_result("earlier", text="Earlier evidence", tokens=tokens, items=items)
    )
    preparation = SemanticRecallPreparation(
        factory, principal(), retriever, DeterministicQueryFormer(principal())
    )

    async def current(*args: Any, **kwargs: Any) -> bool:
        return True  # This fixture's synthetic evidence is immutable.

    retriever.validate_retained = current  # type: ignore[method-assign]
    queries = await preparation.plan(live, checkpoint)
    if tokens == 2000:
        assert queries == []
    else:
        assert len(queries) == 1
        assert queries[0].budget_tokens == 100 and queries[0].max_items == 1


@pytest.mark.parametrize("mode", ["valid", "unoffered", "disabled", "target_disabled", "malformed"])
async def test_deferred_memory_search_prepares_only_the_pinned_authorized_target(mode: str) -> None:
    from agent_core.tools.tool_call import ToolCallTool

    _, factory, _, _, retriever, _ = await runtime_stack()
    search_tool = MemorySearchTool(retriever)
    async with factory() as uow:
        live = await uow.runs.get(RUN_ID, principal())
        checkpoint = await uow.checkpoints.latest(RUN_ID)
        await uow.agents.put(
            agent().model_copy(
                update={
                    "enabled_tools": []
                    if mode == "disabled"
                    else ["tool.call"]
                    if mode == "target_disabled"
                    else ["tool.call", "memory.search"]
                }
            )
        )
    assert checkpoint is not None
    specs = [ToolCallTool.spec, *([] if mode == "unoffered" else [search_tool.spec])]
    checkpoint.pinned_tool_specs = {spec.name: spec for spec in specs}
    checkpoint.pinned_tool_names = list(checkpoint.pinned_tool_specs)
    checkpoint.pinned_tool_versions = {spec.name: spec.version for spec in specs}
    arguments = {"text": "Breakfast constraints", "scope": "general"}
    wrapper: dict[str, Any] = {"name": "memory.search", "arguments": arguments}
    if mode == "malformed":
        wrapper["unexpected"] = True
    checkpoint.pending_tool_calls = [
        ToolCallItem(
            call_id="deferred",
            item_index=0,
            name="tool.call",
            arguments=wrapper,
            raw_arguments=json.dumps(wrapper),
        ).model_dump(mode="json")
    ]
    preparation = SemanticRecallPreparation(
        factory, principal(), retriever, DeterministicQueryFormer(principal())
    )
    planned = await preparation.plan(live, checkpoint)
    assert planned == (
        [
            search_tool.query(
                arguments, tenant_id=principal().tenant_id, principal_id=principal().principal_id
            )
        ]
        if mode == "valid"
        else []
    )


async def test_disabled_direct_search_does_not_prepare_optional_work() -> None:

    _, factory, _, _, retriever, _ = await runtime_stack()
    async with factory() as uow:
        live = await uow.runs.get(RUN_ID, principal())
        checkpoint = await uow.checkpoints.latest(RUN_ID)
        await uow.agents.put(agent().model_copy(update={"enabled_tools": []}))
    assert checkpoint is not None
    checkpoint.pending_tool_calls = [
        ToolCallItem(
            call_id="direct",
            item_index=0,
            name="memory.search",
            arguments={"text": "Breakfast constraints", "scope": "general"},
            raw_arguments="{}",
        ).model_dump(mode="json")
    ]
    preparation = SemanticRecallPreparation(
        factory, principal(), retriever, DeterministicQueryFormer(principal())
    )
    assert await preparation.plan(live, checkpoint) == []
