"""Working-state bounds and assembled-context pair integrity (context-engine.md).

The gate suite exercises the typed control tool and the constraint cap; these
tests hold the remaining documented bounds: open tasks are counted without the
completed ones, a fact repeated with the same provenance is one fact, questions
carry until resolved, the block has a token ceiling, and an assembled request
never carries an unpaired or duplicated tool call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.context.history import select_history, validate_tool_pairs
from agent_core.context.working_state import WorkingStateLimitError, WorkingStateManager
from agent_core.domain.context import TaskStatus, WorkingState
from agent_core.domain.messages import TextPart, ToolCallItem, ToolResultItem
from agent_core.domain.policies import TrustLevel

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
LIMITS = {
    "max_constraints": 20,
    "max_open_tasks": 3,
    "max_established_facts": 40,
    "max_open_questions": 2,
    "block_ceiling_tokens": 1_000,
}


def _manager(clock: FixedClock | None = None, **limits: int) -> WorkingStateManager:
    return WorkingStateManager(
        clock or FixedClock(NOW), {**LIMITS, **limits}, ConservativeTokenEstimator()
    )


def _task(task_id: str, status: str = "open") -> dict[str, Any]:
    return {"task_id": task_id, "description": f"do {task_id}", "status": status}


def test_completed_tasks_do_not_count_against_the_open_task_cap() -> None:
    manager = _manager()
    state = manager.transition(
        WorkingState(),
        {
            "upsert_tasks": [
                *(_task(f"done-{index}", "completed") for index in range(5)),
                *(_task(f"open-{index}") for index in range(3)),
            ]
        },
    )

    assert len(state.tasks) == 8
    assert [task.task_id for task in state.tasks] == sorted(task.task_id for task in state.tasks)
    with pytest.raises(WorkingStateLimitError, match="open-task cap"):
        manager.transition(state, {"upsert_tasks": [_task("open-3")]})
    closed = manager.transition(state, {"upsert_tasks": [_task("open-0", "completed")]})
    reopened = manager.transition(closed, {"upsert_tasks": [_task("open-3")]})
    assert sum(task.status is not TaskStatus.COMPLETED for task in reopened.tasks) == 3


def test_a_repeated_fact_with_the_same_provenance_is_one_fact() -> None:
    clock = FixedClock(NOW)
    manager = _manager(clock)
    first = manager.transition(
        WorkingState(), {"add_facts": [{"statement": "tests pass", "source_event_ids": [9, 7]}]}
    )
    clock.advance(timedelta(minutes=5))

    repeated = manager.transition(
        first,
        {
            "add_facts": [
                {"statement": "tests pass", "source_event_ids": [7, 9, 7]},
                {"statement": "tests pass", "source_event_ids": [8]},
            ]
        },
    )

    assert [(fact.statement, fact.source_event_ids) for fact in repeated.established_facts] == [
        ("tests pass", [7, 9]),
        ("tests pass", [8]),
    ]
    assert repeated.established_facts[0].established_at == NOW
    assert all(
        fact.trust_level is TrustLevel.EXTERNAL_UNTRUSTED for fact in repeated.established_facts
    )


def test_fact_cap_bounds_the_established_facts() -> None:
    manager = _manager(max_established_facts=1)
    one = manager.transition(
        WorkingState(), {"add_facts": [{"statement": "one", "source_event_ids": [1]}]}
    )

    with pytest.raises(WorkingStateLimitError, match="fact cap"):
        manager.transition(one, {"add_facts": [{"statement": "two", "source_event_ids": [2]}]})


def test_questions_are_deduplicated_capped_and_leave_only_when_resolved() -> None:
    manager = _manager()
    asked = manager.add_question(WorkingState(), "Which region?")
    asked = manager.add_question(asked, "Which region?")
    asked = manager.add_question(asked, "Which account?")

    assert asked.open_questions == ["Which region?", "Which account?"]
    with pytest.raises(WorkingStateLimitError, match="question cap"):
        manager.add_question(asked, "Which team?")
    # A model update that touches nothing else never drops an open question.
    assert manager.transition(asked, {"next_action": "wait"}).open_questions == [
        "Which region?",
        "Which account?",
    ]
    resolved = manager.transition(asked, {"resolve_questions": ["Which region?"]})
    assert resolved.open_questions == ["Which account?"]
    assert manager.resolve_question(asked, "Which account?").open_questions == ["Which region?"]
    unchanged = manager.resolve_question(asked, None)
    assert unchanged == asked
    assert unchanged is not asked


@pytest.mark.parametrize("question", ["", "   ", "x" * 4_097])
def test_an_empty_or_oversized_question_is_refused(question: str) -> None:
    with pytest.raises(WorkingStateLimitError, match="empty or too long"):
        _manager().add_question(WorkingState(), question)


def test_the_rendered_block_has_a_token_ceiling() -> None:
    manager = _manager(block_ceiling_tokens=200)

    small = manager.transition(WorkingState(), {"objective": "ship it"})
    assert small.objective == "ship it"
    with pytest.raises(WorkingStateLimitError, match="token ceiling"):
        manager.transition(small, {"objective": "ship the release " * 40})
    with pytest.raises(WorkingStateLimitError, match="token ceiling"):
        manager.add_question(small, "why " * 100)


def test_working_state_round_trips_through_its_checkpoint_container() -> None:
    manager = _manager()
    state = manager.transition(
        WorkingState(),
        {
            "objective": "finish",
            "add_constraints": ["  keep provenance  ", "keep provenance", ""],
            "upsert_tasks": [_task("a")],
            "add_facts": [{"statement": "known", "source_event_ids": [3]}],
        },
    )
    container: dict[str, Any] = {}

    WorkingStateManager.store(container, state)

    assert state.constraints == ["keep provenance"]
    assert WorkingStateManager.load(container) == state
    assert WorkingStateManager.load({}) == WorkingState()


def _call(call_id: str) -> ToolCallItem:
    return ToolCallItem(
        call_id=call_id, item_index=0, name="echo", arguments={}, raw_arguments="{}"
    )


def _result(call_id: str) -> ToolResultItem:
    return ToolResultItem(call_id=call_id, content=[TextPart(text="done")])


@pytest.mark.parametrize(
    ("items", "message"),
    [
        ([_call("a")], "orphaned tool call or result"),
        ([_result("a")], "orphaned tool call or result"),
        ([_call("a"), _result("b")], "orphaned tool call or result"),
        ([_call("a"), _call("a"), _result("a")], "duplicate tool pair"),
        ([_call("a"), _result("a"), _result("a")], "duplicate tool pair"),
    ],
)
def test_assembled_context_rejects_unpaired_or_duplicated_tool_items(
    items: list[ToolCallItem | ToolResultItem], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_tool_pairs(items)


def test_assembled_context_accepts_complete_pairs() -> None:
    validate_tool_pairs([_call("a"), _call("b"), _result("b"), _result("a")])
    validate_tool_pairs([])


@pytest.mark.parametrize(("floor", "budget"), [(-1, 100), (0, -1)])
def test_history_selection_refuses_a_negative_floor_or_budget(floor: int, budget: int) -> None:
    with pytest.raises(ValueError, match="must be nonnegative"):
        select_history([], floor, budget, ConservativeTokenEstimator(), "fake:scripted")
