"""Tool-result truncation moves in blocks so the body's cached prefix survives.

context-engine.md ("Yield order under pressure", item 3) and ADR-0242: when the
aggregate tool-result estimate exceeds its budget, the builder truncates the
oldest results until the aggregate fits and then continues to the next
quarter-budget boundary of truncated tokens. The truncated prefix therefore
changes at most once per quarter budget of new results, not on every step. The
Kindle run of 2026-10-23 truncated one more result on each of thirty-two
consecutive steps, and each rewrite of the oldest bytes cost a full uncached
read of the body behind them.
"""

from __future__ import annotations

from agent_core.context.history import validate_tool_pairs
from agent_core.domain.messages import (
    ConversationItem,
    TextPart,
    ToolCallItem,
    ToolResultItem,
    UserMessage,
)
from agent_core.domain.runs import RunCheckpoint, RunStatus
from tests.contract.support import NOW, agent, principal, run
from tests.gates.test_context_m7 import _builder

TOOL_RESULT_BUDGET = 4_000
# Each result estimates to about 371 tokens (its canonical JSON at three bytes
# per token plus the per-item framing allowance), so ten results fit the budget
# and the truncation block is 1,000 tokens.
RESULT_BYTES = 900


def _conversation(result_count: int) -> list[ConversationItem]:
    items: list[ConversationItem] = [
        UserMessage(content=[TextPart(text="Compile the list")], source_event_sequence=1)
    ]
    sequence = 2
    for index in range(result_count):
        call_id = f"call-{index:02d}"
        items.append(
            ToolCallItem(
                call_id=call_id,
                item_index=index,
                name="browser.observe",
                arguments={},
                raw_arguments="{}",
                source_event_sequence=sequence,
            )
        )
        items.append(
            ToolResultItem(
                call_id=call_id,
                content=[TextPart(text=f"page {index:02d} " + ("x" * RESULT_BYTES))],
                source_event_sequence=sequence + 1,
            )
        )
        sequence += 2
    return items


def _checkpoint(result_count: int) -> RunCheckpoint:
    return RunCheckpoint(
        run_id=run(status=RunStatus.RUNNING).id,
        version=result_count,
        status=RunStatus.RUNNING,
        conversation=_conversation(result_count),
        created_at=NOW,
    )


async def _truncated_call_ids(result_count: int) -> list[str]:
    builder = _builder(tool_results=TOOL_RESULT_BUDGET)
    active_run = run(status=RunStatus.RUNNING)
    request = await builder.build(active_run, _checkpoint(result_count), agent(), principal())
    body = request.conversation[int(request.metadata["region_a_items"]) :]
    validate_tool_pairs(body)
    truncated: list[str] = []
    for item in body:
        if not isinstance(item, ToolResultItem):
            continue
        text = item.content[0]
        assert isinstance(text, TextPart)
        if "tool result truncated" in text.text:
            truncated.append(item.call_id)
    return truncated


async def test_first_truncation_moves_to_the_quarter_budget_boundary() -> None:
    # Ten results fit; the eleventh needs one truncation (about 371 tokens),
    # which rounds up to the 1,000-token block: the three oldest.
    assert await _truncated_call_ids(10) == []
    assert await _truncated_call_ids(11) == ["call-00", "call-01", "call-02"]


async def test_truncated_prefix_is_stable_across_steps_within_a_block() -> None:
    # Each later step needs one more result truncated than the last; the block
    # boundary keeps the same oldest results truncated, so the bytes before the
    # newest exchange do not change between consecutive requests.
    first = await _truncated_call_ids(11)
    assert await _truncated_call_ids(12) == first
    second = await _truncated_call_ids(13)
    assert len(second) == 6
    assert await _truncated_call_ids(14) == second
    assert await _truncated_call_ids(15) == second


async def test_truncated_prefix_advances_one_block_at_a_time() -> None:
    # The sixteenth result needs six truncated (about 2,226 tokens), which
    # rounds up to the 3,000-token boundary: nine results, still oldest first.
    assert await _truncated_call_ids(16) == [f"call-{index:02d}" for index in range(9)]
