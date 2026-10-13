"""Automatic and deliberate evidence share the same rendered context allowance."""

import hashlib
from collections.abc import Sequence
from typing import Any
from uuid import UUID

import pytest

from agent_core.context.recall_budget import OMITTED, select_search_results
from agent_core.domain.messages import (
    ConversationItem,
    MemoryRecallUsage,
    TextPart,
    ToolCallItem,
    ToolResultItem,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import RunStatus
from tests.contract.support import agent, principal, run
from tests.unit.test_context_recall_delta import (
    _builder,
    _checkpoint,
    _correction_line,
    _Retriever,
)


class CurrentRetriever(_Retriever):
    async def validate_retained(self, usage: MemoryRecallUsage, **_: Any) -> bool:
        return True


def tool_result(items: Sequence[ConversationItem], index: int) -> ToolResultItem:
    item = items[index]
    assert isinstance(item, ToolResultItem)
    return item


def call_ids(items: Sequence[ConversationItem]) -> list[str]:
    result = []
    for item in items:
        assert isinstance(item, (ToolCallItem, ToolResultItem))
        result.append(item.call_id)
    return result


async def test_actual_search_exhaustion_keeps_mandatory_corrections_and_explains_limit() -> None:
    from agent_core.domain.recall_execution import EXHAUSTED, RecallBudgetExceededError

    class Exhausted(CurrentRetriever):
        async def recall(self, *args: Any, **kwargs: Any) -> Any:
            raise RecallBudgetExceededError(EXHAUSTED)

    retriever = Exhausted()
    request = await _builder(retriever).build(
        run(status=RunStatus.RUNNING), _checkpoint(), agent(), principal()
    )
    assert EXHAUSTED in request.model_dump_json()
    assert _correction_line() in request.model_dump_json()
    assert len(retriever.correction_calls) == 1


def test_runtime_ranking_records_actual_block_priority_and_one_deduplicated_prefix() -> None:
    from agent_core.context.recall_budget import context_ranking
    from agent_core.domain.messages import RecallRankingBlock

    old_call, old_result = search_result("older", text="Older evidence", tokens=50, items=2)
    new_call, new_result = search_result("newer", text="Newer evidence", tokens=50, items=2)
    assert old_result.memory_recall is not None and new_result.memory_recall is not None
    old_result.memory_recall = old_result.memory_recall.model_copy(
        update={"trace_id": UUID(int=101), "ranked_ids": (UUID(int=1), UUID(int=2))}
    )
    new_result.memory_recall = new_result.memory_recall.model_copy(
        update={"trace_id": UUID(int=102), "ranked_ids": (UUID(int=2), UUID(int=3))}
    )
    automatic = RecallRankingBlock(
        trace_id=UUID(int=103), content_sha256="a" * 64, ranked_ids=(UUID(int=4), UUID(int=1))
    )
    ranked = context_ranking([old_call, old_result, new_call, new_result], [automatic])
    assert ranked is not None
    assert [block.trace_id.int for block in ranked.blocks] == [102, 101, 103]
    assert [key.int for key in ranked.ranked_ids] == [2, 3, 1, 4]
    # A truncation or an unknown automatic read cannot reuse the old receipt.
    old_result.content = [TextPart(text="Excerpt")]
    assert context_ranking([old_call, old_result], []) is None
    assert context_ranking([], [None]) is None


def test_assembly_rank_has_one_twenty_item_ceiling_across_multiple_blocks() -> None:
    from agent_core.context.recall_budget import context_ranking
    from agent_core.domain.messages import RecallRankingBlock

    blocks = [
        RecallRankingBlock(
            trace_id=UUID(int=1000 + index),
            content_sha256="a" * 64,
            ranked_ids=tuple(UUID(int=value) for value in range(index * 20 + 1, index * 20 + 21)),
        )
        for index in range(4)
    ]
    ranked = context_ranking([], blocks)
    assert ranked is not None
    assert ranked.ranked_ids == blocks[0].ranked_ids


def search_result(
    call_id: str, *, text: str, tokens: int, items: int
) -> tuple[ToolCallItem, ToolResultItem]:
    return (
        ToolCallItem(
            call_id=call_id,
            item_index=0,
            name="memory.search",
            arguments={"text": "Needed evidence", "scope": "general"},
            raw_arguments="{}",
        ),
        ToolResultItem(
            call_id=call_id,
            content=[TextPart(text=text)],
            trust=TrustLevel.MEMORY,
            memory_recall=MemoryRecallUsage(
                trace_id=UUID(int=123),
                tokens=tokens,
                items=items,
                content_sha256=hashlib.sha256(text.encode()).hexdigest(),
            ),
        ),
    )


@pytest.mark.parametrize("tokens,items", [(2000, 2), (100, 20)])
async def test_search_exhaustion_skips_automatic_evidence_but_keeps_corrections(
    tokens: int, items: int
) -> None:
    retriever = CurrentRetriever()
    checkpoint = _checkpoint()
    checkpoint.conversation.extend(
        search_result("one", text="Deliberate evidence", tokens=tokens, items=items)
    )
    original = checkpoint.model_dump_json()
    request = await _builder(retriever).build(
        run(status=RunStatus.RUNNING), checkpoint, agent(), principal()
    )
    assert retriever.calls == 0, "A search result must consume the shared automatic allowance"
    assert len(retriever.correction_calls) == 1
    assert _correction_line() in request.model_dump_json()
    assert checkpoint.model_dump_json() == original


async def test_search_counts_reduce_both_automatic_token_and_item_allowances() -> None:
    retriever = CurrentRetriever()
    checkpoint = _checkpoint()
    checkpoint.conversation.extend(
        search_result("one", text="Deliberate evidence", tokens=1900, items=19)
    )
    await _builder(retriever).build(run(status=RunStatus.RUNNING), checkpoint, agent(), principal())
    assert retriever.queries[0].budget_tokens == 100
    assert retriever.queries[0].max_items == 1
    assert retriever.calls == 1, "The base used the last item, leaving no delta allowance"


@pytest.mark.parametrize("deferred", [False, True])
def test_newest_whole_search_blocks_share_caps_and_keep_call_pairs(deferred: bool) -> None:
    pairs = [search_result(str(i), text=f"Evidence {i}", tokens=900, items=9) for i in range(3)]
    if deferred:
        pairs = [
            (
                call.model_copy(
                    update={"name": "tool.call", "arguments": {"name": "memory.search"}}
                ),
                result,
            )
            for call, result in pairs
        ]
    original = [item for pair in pairs for item in pair]
    selected, remaining, omitted = select_search_results(original)
    assert omitted
    assert remaining.tokens == 200 and remaining.items == 2
    assert tool_result(selected, 1).content == [TextPart(text=OMITTED)]
    assert tool_result(selected, 3).content == tool_result(original, 3).content
    assert tool_result(selected, 5).content == tool_result(original, 5).content
    assert tool_result(original, 1).content == [TextPart(text="Evidence 0")]
    assert call_ids(selected) == call_ids(original)
    assert all(tool_result(selected, i).trust is TrustLevel.MEMORY for i in (1, 3, 5))


def test_repeated_blocks_use_one_allowance_after_checkpoint_replay() -> None:
    checkpoint = _checkpoint()
    for key in ("one", "two"):
        checkpoint.conversation.extend(
            search_result(key, text="Repeated evidence", tokens=300, items=2)
        )
    from agent_core.domain.runs import RunCheckpoint

    restored = RunCheckpoint.model_validate_json(checkpoint.model_dump_json())
    first = select_search_results(checkpoint.conversation)
    assert first == select_search_results(restored.conversation)
    assert first[1].tokens == 1700 and first[1].items == 18


@pytest.mark.parametrize(
    "change", ["digest", "excerpt", "underreported", "legacy", "oversized_legacy"]
)
def test_changed_bytes_and_unreceipted_results_cannot_borrow_a_small_allowance(change: str) -> None:
    call, result = search_result("one", text="Evidence", tokens=10, items=1)
    if change == "digest":
        result.content = [TextPart(text="Different evidence")]
    elif change == "excerpt":
        result.context_content = [TextPart(text="[Excerpt]")]
    elif change == "underreported":
        text = "x" * 4000
        result.content = [TextPart(text=text)]
        result.memory_recall = MemoryRecallUsage(
            tokens=1, items=1, content_sha256=hashlib.sha256(text.encode()).hexdigest()
        )
    else:
        result.memory_recall = None
        if change == "oversized_legacy":
            result.content = [TextPart(text="x" * 8001)]
    selected, remaining, omitted = select_search_results([call, result])
    if change in {"digest", "excerpt", "oversized_legacy"}:
        assert omitted and tool_result(selected, 1).content == [TextPart(text=OMITTED)]
        assert remaining.tokens == 2000 and remaining.items == 20
    elif change == "legacy":
        assert not omitted and remaining.tokens == remaining.items == 0
    else:
        assert not omitted and remaining.tokens == 1000


def test_another_tool_cannot_supply_a_memory_budget_receipt() -> None:
    call, result = search_result("other", text="Other tool result", tokens=2000, items=20)
    call.name = "math.calculate"
    _, remaining, omitted = select_search_results([call, result])
    assert not omitted and remaining.tokens == 2000 and remaining.items == 20


@pytest.mark.parametrize("mode", ["missing", "failure", "invalid", "old_trace"])
async def test_unknown_search_evidence_cannot_consume_current_allowance(mode: str) -> None:
    from agent_core.context.recall_budget import select_current_search_results
    from tests.contract.support import RUN_ID, SESSION_ID

    call, result = search_result("one", text="Old evidence", tokens=2000, items=20)
    if mode == "missing":
        result.memory_recall = None
    elif mode == "old_trace":
        assert result.memory_recall is not None
        result.memory_recall = result.memory_recall.model_copy(update={"trace_id": None})

    class Validator:
        async def validate_retained(self, *_: Any, **kwargs: Any) -> bool:
            if mode == "failure":
                raise RuntimeError("Unavailable local validation")
            return False

    selected, remaining, omitted = await select_current_search_results(
        [call, result], Validator(), session_id=SESSION_ID, run_id=RUN_ID
    )
    assert omitted and tool_result(selected, 1).content == [TextPart(text=OMITTED)]
    assert remaining.tokens == 2000 and remaining.items == 20


async def test_only_four_builtin_blocks_can_start_dependency_validation() -> None:
    from agent_core.context.recall_budget import select_current_search_results
    from tests.contract.support import RUN_ID, SESSION_ID

    conversation: list[ConversationItem] = []
    for i in range(7):
        conversation.extend(search_result(str(i), text=f"Evidence {i}", tokens=10, items=1))
    other, other_result = search_result("foreign", text="Foreign tool", tokens=1, items=1)
    other.name = "other.tool"
    conversation.extend([other, other_result])
    calls = []

    class Validator:
        async def validate_retained(self, usage: MemoryRecallUsage, **_: Any) -> bool:
            calls.append(usage)
            return True

    selected, remaining, _ = await select_current_search_results(
        conversation, Validator(), session_id=SESSION_ID, run_id=RUN_ID
    )
    assert len(calls) == 4
    assert remaining.tokens == 1960 and remaining.items == 16
    assert tool_result(selected, -1).content == other_result.content


async def test_invalid_receipt_cannot_borrow_another_receipts_trace_permission() -> None:
    from agent_core.context.recall_budget import select_current_search_results
    from tests.contract.support import RUN_ID, SESSION_ID

    valid = search_result("valid", text="Current evidence", tokens=10, items=1)
    forged = search_result("forged", text="Different payload", tokens=10, items=1)

    class Validator:
        async def validate_retained(self, usage: MemoryRecallUsage, **_: Any) -> bool:
            return usage == valid[1].memory_recall

    selected, remaining, _ = await select_current_search_results(
        [*valid, *forged], Validator(), session_id=SESSION_ID, run_id=RUN_ID
    )
    assert tool_result(selected, 1).content == valid[1].content
    assert tool_result(selected, 3).content == [TextPart(text=OMITTED)]
    assert remaining.items == 19
