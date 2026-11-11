"""Saved deliberate evidence is checked against its current authoritative support."""

from typing import Any, cast
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.domain.memory import RecalledBelief
from agent_core.domain.messages import MemoryRecallUsage
from agent_core.memory.retrieval import HybridMemoryRetriever
from agent_core.memory.source_recovery import (
    BoundedSourceRecovery,
    SourceRecoveryUnavailableError,
)
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.memory_fixtures import formation_stack, memory, recall_query, user_event
from tests.contract.support import RUN_ID, SESSION_ID, principal


@pytest.mark.parametrize("mode", ["current", "legacy", "as_of", "known_at", "changed", "tampered"])
async def test_repeated_current_search_preserves_automatic_allowance(mode: str) -> None:
    from datetime import timedelta

    from agent_core.context.recall_budget import (
        AUTOMATIC_RESERVE,
        OMITTED,
        select_current_search_results,
    )
    from agent_core.domain.messages import ConversationItem, TextPart
    from agent_core.tools.memory_search import MemorySearchTool
    from tests.contract.support import tool_context
    from tests.unit.test_context_memory_budget import search_result, tool_result

    clock, factory, _, _ = await formation_stack()
    record = memory(statement="The workshop is on Thursday.")
    async with factory() as uow:
        await uow.memories.upsert_belief(record)
    retriever = HybridMemoryRetriever(
        factory, clock, SequenceIdFactory(UUID(int=i) for i in range(8000, 8010)), principal()
    )
    tool = MemorySearchTool(
        retriever,
        model_planning=True,
        overview_enabled=True,
        retain_automatic=True,
        retain_search_history=mode != "legacy",
    )
    conversation: list[ConversationItem] = []
    usages = []
    for index, call_id in enumerate(("first", "second")):
        if mode == "changed" and index:
            async with factory() as uow:
                await uow.memories.upsert_belief(
                    memory(belief_id=99, statement="The workshop venue is the library.")
                )
        arguments = {"text": "workshop", "scope": record.scope}
        if mode in {"as_of", "known_at"}:
            arguments[mode] = clock.now().isoformat()
        found = await tool.execute(arguments, tool_context())
        assert found.ok and found.memory_recall is not None
        assert found.memory_recall.items == (2 if mode == "changed" and index else 1)
        assert await retriever.validate_retained(
            found.memory_recall, session_id=SESSION_ID, run_id=RUN_ID
        )
        if mode == "tampered" and index:
            found.memory_recall = found.memory_recall.model_copy(
                update={"current_evidence_sha256": "0" * 64}
            )
        usages.append(found.memory_recall)
        pair = search_result(call_id, text="", tokens=1, items=1)
        pair[1].content = found.content
        pair[1].memory_recall = found.memory_recall
        conversation.extend(pair)
        clock.advance(timedelta(seconds=1))
    assert usages[0].content_sha256 != usages[1].content_sha256
    selected, allowance, _ = await select_current_search_results(
        conversation,
        retriever,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        reserve=AUTOMATIC_RESERVE,
    )
    if mode == "current":
        assert tool_result(selected, 1).content == [TextPart(text=OMITTED)]
        assert tool_result(selected, 3).content == tool_result(conversation, 3).content
        retained = usages[-1:]
    elif mode == "tampered":
        assert tool_result(selected, 1).content == tool_result(conversation, 1).content
        assert tool_result(selected, 3).content == [TextPart(text=OMITTED)]
        retained = usages[:1]
    else:
        assert selected == conversation
        retained = usages
    assert allowance.items == 20 - sum(usage.items for usage in retained)
    assert allowance.tokens == 2000 - sum(usage.tokens for usage in retained)


@pytest.mark.parametrize("changed", [False, True])
async def test_retained_partial_source_package_validates_only_its_rendered_quotes(
    changed: bool,
) -> None:
    clock, factory, _, _ = await formation_stack()
    await retained_source_package(changed, clock, factory)


async def retained_source_package(
    changed: bool, clock: FixedClock, factory: UnitOfWorkFactory
) -> None:
    sequences = [await user_event(factory, "Workshop detail: " + str(i) * 700) for i in range(2)]
    record = memory(statement="The workshop is on Thursday.").model_copy(
        update={
            "subject": "workshop",
            "scope": "general",
            "source_event_ids": sequences,
        }
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(record)
    admitted = [True]
    retriever = HybridMemoryRetriever(
        factory,
        clock,
        SequenceIdFactory(UUID(int=i) for i in range(8000, 8010)),
        principal(),
        source_recovery=BoundedSourceRecovery(principal(), admitted=lambda: admitted[0]),
    )
    query = recall_query(text="workshop", budget_tokens=400).model_copy(
        update={"current_scope": "general"}
    )
    result = await retriever.recall(query, session_id=SESSION_ID, run_id=RUN_ID)
    assert isinstance(result.items[0], RecalledBelief)
    assert len(result.items[0].sources) == 1, "This budget admits one of two whole quotes"
    async with factory() as uow:
        trace = await uow.traces.get(result.trace_id, principal())
    usage = MemoryRecallUsage(
        trace_id=result.trace_id,
        tokens=result.tokens,
        items=len(result.items),
        content_sha256=trace.rendered_sha256,
    )
    if changed:
        admitted[0] = False
    assert await retriever.validate_retained(usage, session_id=SESSION_ID, run_id=RUN_ID) is (
        not changed
    )


@pytest.mark.parametrize("change", [None, "erasure", "admission"])
async def test_retained_semantic_summary_validates_support_without_rescoring_relevance(
    change: str | None,
) -> None:
    from agent_core.adapters.determinism import RandomIdFactory
    from agent_core.memory.profiles import DEFAULT_RETRIEVAL_PROFILE
    from tests.contract.reconsolidation_summary_recall_cases import summary_recall_inputs
    from tests.contract.semantic_memory_cases import SPACE, VECTOR
    from tests.contract.support import memory_uow_factory
    from tests.contract.test_semantic_recall import LocalQuery

    clock, factory = await memory_uow_factory()
    operation, originals = await summary_recall_inputs(factory)
    async with factory() as uow:
        for original in originals:
            source = await uow.semantic_memory.capture(principal(), original.id, SPACE)
            assert source is not None and await uow.semantic_memory.put(principal(), source, VECTOR)
    admitted = [True]
    retriever = HybridMemoryRetriever(
        factory,
        clock,
        RandomIdFactory(),
        principal(),
        profile=DEFAULT_RETRIEVAL_PROFILE.model_copy(update={"semantic_enabled": True}),
        reconsolidation_enabled=lambda: admitted[0],
        semantic_queries=LocalQuery(),
    )
    query = recall_query(text="Alternative recommendations", max_items=4).model_copy(
        update={"candidate_selection": "relevance"}
    )
    result = await retriever.recall(query, session_id=SESSION_ID, run_id=RUN_ID)
    assert operation.id in {item.belief_id for item in result.items}
    async with factory() as uow:
        trace = await uow.traces.get(result.trace_id, principal())
        if change == "erasure":
            await uow.memories.fence_for_erasure(principal(), [originals[-1].id])
    if change == "admission":
        admitted[0] = False
    usage = MemoryRecallUsage(
        trace_id=result.trace_id,
        tokens=result.tokens,
        items=len(result.items),
        content_sha256=trace.rendered_sha256,
    )
    assert await retriever.validate_retained(usage, session_id=SESSION_ID, run_id=RUN_ID) is (
        change is None
    )


async def test_missing_trace_does_not_discard_later_valid_search_results() -> None:
    from agent_core.context.recall_budget import OMITTED, select_current_search_results
    from agent_core.domain.messages import TextPart
    from tests.unit.test_context_memory_budget import search_result, tool_result

    clock, factory, _, _ = await formation_stack()
    record = memory(statement="The workshop is on Thursday.")
    async with factory() as uow:
        await uow.memories.upsert_belief(record)
    retriever = HybridMemoryRetriever(
        factory, clock, SequenceIdFactory(UUID(int=i) for i in range(8000, 8010)), principal()
    )
    result = await retriever.recall(
        recall_query(text="workshop"), session_id=SESSION_ID, run_id=RUN_ID
    )
    assert result.items
    async with factory() as uow:
        trace = await uow.traces.get(result.trace_id, principal())
    missing = search_result("missing", text="Erased evidence", tokens=5, items=1)
    valid = search_result(
        "valid", text=result.rendered, tokens=result.tokens, items=len(result.items)
    )
    valid[1].memory_recall = MemoryRecallUsage(
        trace_id=result.trace_id,
        tokens=result.tokens,
        items=len(result.items),
        content_sha256=trace.rendered_sha256,
    )
    selected, _, _ = await select_current_search_results(
        [*missing, *valid], retriever, session_id=SESSION_ID, run_id=RUN_ID
    )
    assert tool_result(selected, 1).content == [TextPart(text=OMITTED)]
    assert tool_result(selected, 3).content == valid[1].content


async def test_unavailable_source_recovery_withholds_only_its_receipt() -> None:
    clock, factory, _, _ = await formation_stack()
    sequences = [await user_event(factory, "Workshop detail: " + "4" * 700)]
    record = memory(statement="The workshop is on Thursday.").model_copy(
        update={"subject": "workshop", "scope": "general", "source_event_ids": sequences}
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(record)

    class InterruptibleRecovery(BoundedSourceRecovery):
        available = True

        async def recover(self, *args: Any, **kwargs: Any) -> Any:
            if not self.available:
                raise SourceRecoveryUnavailableError
            return await super().recover(*args, **kwargs)

    recovery = InterruptibleRecovery(principal(), admitted=lambda: True)
    retriever = HybridMemoryRetriever(
        factory,
        clock,
        SequenceIdFactory(UUID(int=i) for i in range(8100, 8110)),
        principal(),
        source_recovery=recovery,
    )
    query = recall_query(text="workshop", budget_tokens=900).model_copy(
        update={"current_scope": "general"}
    )
    result = await retriever.recall(query, session_id=SESSION_ID, run_id=RUN_ID)
    assert isinstance(result.items[0], RecalledBelief) and result.items[0].sources
    async with factory() as uow:
        trace = await uow.traces.get(result.trace_id, principal())
    usage = MemoryRecallUsage(
        trace_id=result.trace_id,
        tokens=result.tokens,
        items=len(result.items),
        content_sha256=trace.rendered_sha256,
    )
    recovery.available = False
    # Fail closed for this receipt rather than abort the shared validation pass.
    assert await retriever.validate_retained(usage, session_id=SESSION_ID, run_id=RUN_ID) is False


async def test_lexical_receipt_does_not_wait_on_semantic_authority() -> None:
    clock, factory, _, _ = await formation_stack()
    async with factory() as uow:
        await uow.memories.upsert_belief(
            memory(statement="The workshop is on Thursday.").model_copy(
                update={"subject": "workshop", "scope": "general"}
            )
        )
    ids = SequenceIdFactory(UUID(int=i) for i in range(8200, 8220))
    lexical = HybridMemoryRetriever(factory, clock, ids, principal())
    query = recall_query(text="workshop", budget_tokens=400).model_copy(
        update={"current_scope": "general"}
    )
    result = await lexical.recall(query, session_id=SESSION_ID, run_id=RUN_ID)
    async with factory() as uow:
        trace = await uow.traces.get(result.trace_id, principal())
    assert "+semantic@" not in trace.retrieval_policy_version
    usage = MemoryRecallUsage(
        trace_id=result.trace_id,
        tokens=result.tokens,
        items=len(result.items),
        content_sha256=trace.rendered_sha256,
    )

    class UnreachableAuthority:
        async def validate_authority(self, **_: Any) -> bool:
            raise TimeoutError

    validating = HybridMemoryRetriever(
        factory,
        clock,
        ids,
        principal(),
        runtime_semantic_queries=cast(Any, UnreachableAuthority()),
    )
    assert await validating.validate_retained(usage, session_id=SESSION_ID, run_id=RUN_ID)


@pytest.mark.parametrize("tampered", [False, True])
async def test_returned_ids_must_match_authoritative_rendered_trace(tampered: bool) -> None:
    clock, factory, _, _ = await formation_stack()
    record = memory(statement="The workshop is on Thursday.")
    async with factory() as uow:
        await uow.memories.upsert_belief(record)
    retriever = HybridMemoryRetriever(
        factory, clock, SequenceIdFactory(UUID(int=i) for i in range(8000, 8010)), principal()
    )
    result = await retriever.recall(
        recall_query(text="workshop"), session_id=SESSION_ID, run_id=RUN_ID
    )
    assert result.items
    async with factory() as uow:
        trace = await uow.traces.get(result.trace_id, principal())
    usage = MemoryRecallUsage(
        trace_id=result.trace_id,
        tokens=result.tokens,
        items=len(result.items),
        content_sha256=trace.rendered_sha256,
        returned_ids=(UUID(int=999),) if tampered else tuple(trace.returned),
    )
    assert await retriever.validate_retained(usage, session_id=SESSION_ID, run_id=RUN_ID) is (
        not tampered
    )
