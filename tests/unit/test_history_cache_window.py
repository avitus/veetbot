"""Section 10.1's rolling prompt-cache window over the conversation history."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import agent_core.context.planner as planner_module
from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.anthropic_messages import AnthropicMessagesProvider
from agent_core.adapters.persistence.memory import (
    InMemoryAgentRepository,
    InMemoryToolInvocationRepository,
)
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.bootstrap import _memory_uow_repositories
from agent_core.context.builder import BudgetedContextBuilder, MinimalContextBuilder
from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.context.planner import EventContextPlanner
from agent_core.context.working_state import WorkingStateManager
from agent_core.domain.messages import (
    AssistantMessage,
    CacheBreakpoint,
    CacheHints,
    ConversationItem,
    ModelLimits,
    ModelRequest,
    ProviderReasoningItem,
    ResolvedModel,
    SystemMessage,
    TextPart,
    ToolCallItem,
    ToolResultItem,
    UserMessage,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import ProviderContinuation, Run, RunCheckpoint, RunStatus
from agent_core.tools.calculator import CalculatorTool
from agent_core.tools.registry import StaticToolRegistry
from tests.contract.support import NOW, agent, memory_stack, principal, run, session

CONFIG = yaml.safe_load(
    (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text(encoding="utf-8")
)


def _claude() -> ResolvedModel:
    return ResolvedModel(
        provider="anthropic",
        model="claude-fable-5-1",
        limits=ModelLimits(max_cache_breakpoints=4),
        resolved_at=NOW,
    )


async def _factory() -> tuple[FixedClock, MemoryUnitOfWorkFactory]:
    clock, sessions, runs, events = await memory_stack()
    factory = MemoryUnitOfWorkFactory(
        _memory_uow_repositories(
            agents=InMemoryAgentRepository(),
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=InMemoryToolInvocationRepository(runs),
            clock=clock,
        )
    )
    return clock, factory


def _planner(clock: FixedClock, factory: MemoryUnitOfWorkFactory) -> EventContextPlanner:
    return EventContextPlanner(
        factory,
        StaticToolRegistry(),
        ConservativeTokenEstimator(),
        clock,
        principal(),
        CONFIG,
        policy_version="contract-policy@1",
    )


async def _stack() -> tuple[EventContextPlanner, BudgetedContextBuilder]:
    clock, factory = await _factory()
    planner = _planner(clock, factory)
    estimator = ConservativeTokenEstimator()
    builder = BudgetedContextBuilder(
        planner,
        estimator,
        clock,
        WorkingStateManager(clock, CONFIG["working_state"], estimator),
    )
    return planner, builder


def _second_run() -> Run:
    # Events below the seed sequence are the session's history; the rest is this run.
    return run(status=RunStatus.RUNNING).model_copy(update={"seed_event_sequence": 3})


def _checkpoint(active_run: Run, conversation: list[ConversationItem]) -> RunCheckpoint:
    return RunCheckpoint(
        run_id=active_run.id,
        version=1,
        status=RunStatus.RUNNING,
        conversation=conversation,
        created_at=NOW,
    )


def _history() -> list[ConversationItem]:
    return [
        UserMessage(content=[TextPart(text="Plan the trip.")], source_event_sequence=1),
        AssistantMessage(content=[TextPart(text="Here is a plan.")], source_event_sequence=2),
    ]


async def test_planned_cache_hints_carry_a_history_window_for_a_multi_turn_session() -> None:
    planner, builder = await _stack()
    await planner.plan(session(), agent(), principal(), _claude())
    second = _second_run()
    checkpoint = _checkpoint(
        second,
        [
            *_history(),
            UserMessage(content=[TextPart(text="Now book it.")], source_event_sequence=3),
        ],
    )

    request = await builder.build(second, checkpoint, agent(), principal())

    assert request.cache_hints is not None
    boundaries = [hint.boundary for hint in request.cache_hints.breakpoints]
    assert boundaries[:2] == ["after_system", "after_tools"]
    assert "after_history_prefix" in boundaries


def _call(call_id: str, sequence: int) -> ToolCallItem:
    return ToolCallItem(
        call_id=call_id,
        item_index=0,
        name="browser.snapshot",
        arguments={},
        raw_arguments="{}",
        source_event_sequence=sequence,
    )


def _result(call_id: str, text: str, sequence: int) -> ToolResultItem:
    return ToolResultItem(
        call_id=call_id,
        content=[TextPart(text=text)],
        trust=TrustLevel.EXTERNAL_UNTRUSTED,
        source_event_sequence=sequence,
    )


def _continuation(signature: str) -> ProviderContinuation:
    # The runtime keeps only the latest turn's signed reasoning (loop.py).
    reasoning = ProviderReasoningItem(
        item_index=0,
        provider="anthropic",
        provider_payload={"type": "thinking", "thinking": "", "signature": signature},
    )
    return ProviderContinuation(provider="anthropic", opaque_items=[reasoning.model_dump()])


def _this_run() -> list[ConversationItem]:
    return [
        UserMessage(content=[TextPart(text="Now book it.")], source_event_sequence=3),
        AssistantMessage(content=[TextPart(text="Opening the site.")], source_event_sequence=4),
        _call("call-1", 5),
        _result("call-1", "page one", 6),
    ]


def _history_marks(request: ModelRequest) -> list[int]:
    assert request.cache_hints is not None
    marks = [
        hint.through_item
        for hint in request.cache_hints.breakpoints
        if hint.boundary == "after_history_prefix"
    ]
    assert all(mark is not None for mark in marks)
    return [mark for mark in marks if mark is not None]


def _dumped(items: list[ConversationItem]) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in items]


async def test_each_history_marker_closes_a_prefix_a_later_request_repeats() -> None:
    planner, builder = await _stack()
    await planner.plan(session(), agent(), principal(), _claude())
    second = _second_run()
    step = await builder.build(
        second,
        _checkpoint(second, [*_history(), *_this_run()]).model_copy(
            update={"provider_continuation": _continuation("turn-1")}
        ),
        agent(),
        principal(),
    )
    next_step = await builder.build(
        second,
        _checkpoint(
            second,
            [
                *_history(),
                *_this_run(),
                AssistantMessage(content=[TextPart(text="Filling the form.")]),
                _call("call-2", 8),
                _result("call-2", "page two", 9),
            ],
        ).model_copy(update={"provider_continuation": _continuation("turn-2")}),
        agent(),
        principal(),
    )
    third = run(status=RunStatus.RUNNING).model_copy(update={"seed_event_sequence": 10})
    next_run = await builder.build(
        third,
        _checkpoint(
            third,
            [
                *_history(),
                *_this_run(),
                AssistantMessage(content=[TextPart(text="Booked.")], source_event_sequence=7),
                UserMessage(content=[TextPart(text="Thanks.")], source_event_sequence=10),
            ],
        ),
        agent(),
        principal(),
    )

    assert step.cache_hints is not None
    assert [hint.boundary for hint in step.cache_hints.breakpoints] == [
        "after_system",
        "after_tools",
        "after_history_prefix",
        "after_history_prefix",
    ]
    carried, stable = _history_marks(step)
    region_a = int(step.metadata["region_a_items"])
    assert carried == region_a + len(_history()) - 1
    assert isinstance(step.conversation[stable + 1], ProviderReasoningItem)
    # The next step of the run repeats everything before this turn's reasoning,
    # and the next run repeats the history this run carried in. Each marker is a
    # cache entry some later request reads.
    assert _dumped(next_step.conversation[: stable + 1]) == _dumped(step.conversation[: stable + 1])
    assert _dumped(next_run.conversation[: carried + 1]) == _dumped(
        step.conversation[: carried + 1]
    )
    # A marker on the final block would not be read: the next step drops this
    # turn's reasoning, and the next run moves every row after its history.
    whole = len(step.conversation)
    assert _dumped(next_step.conversation[:whole]) != _dumped(step.conversation)
    assert _dumped(next_run.conversation[: stable + 1]) != _dumped(step.conversation[: stable + 1])
    assert _history_marks(next_step)[0] == carried
    assert _history_marks(next_run)[0] > carried


async def test_a_first_run_marks_only_its_own_prefix() -> None:
    planner, builder = await _stack()
    await planner.plan(session(), agent(), principal(), _claude())
    first = run(status=RunStatus.RUNNING)
    request = await builder.build(
        first,
        _checkpoint(first, [UserMessage(content=[TextPart(text="Plan the trip.")])]),
        agent(),
        principal(),
    )

    assert _history_marks(request) == [len(request.conversation) - 1]


async def test_the_milestone_1_builder_marks_the_prefix_before_its_runtime_row() -> None:
    clock, _sessions, _runs, _events = await memory_stack()
    builder = MinimalContextBuilder(StaticToolRegistry(), clock)
    active = run(status=RunStatus.RUNNING)
    loop = [*_history(), *_this_run()]
    step = await builder.build(
        active,
        _checkpoint(active, loop).model_copy(
            update={"provider_continuation": _continuation("turn-1")}
        ),
        agent(),
        principal(),
    )
    next_step = await builder.build(
        active,
        _checkpoint(
            active,
            [
                *loop,
                AssistantMessage(content=[TextPart(text="Filling the form.")]),
                _call("call-2", 8),
                _result("call-2", "page two", 9),
            ],
        ).model_copy(update={"provider_continuation": _continuation("turn-2")}),
        agent(),
        principal(),
    )

    marks = _history_marks(step)
    assert len(marks) == 1
    assert isinstance(step.conversation[marks[0] + 1], ProviderReasoningItem)
    assert _dumped(next_step.conversation[: marks[0] + 1]) == _dumped(
        step.conversation[: marks[0] + 1]
    )


async def test_a_plan_from_before_the_window_gains_it_without_rotating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, factory = await _factory()
    monkeypatch.setattr(planner_module, "CACHE_BREAKPOINTS", planner_module.CACHE_BREAKPOINTS[:2])
    legacy = await _planner(clock, factory).plan(session(), agent(), principal(), _claude())
    monkeypatch.undo()

    planner = _planner(clock, factory)
    loaded = await planner.current(session().id)
    reused = await planner.plan(session(), agent(), principal(), _claude())
    rotated = await planner.rotate(session().id, "contract-test")
    fresh_clock, fresh_factory = await _factory()
    fresh = await _planner(fresh_clock, fresh_factory).plan(
        session(), agent(), principal(), _claude()
    )

    assert [item.boundary for item in legacy.cache_breakpoints] == ["after_system", "after_tools"]
    assert loaded is not None
    assert [item.boundary for item in loaded.cache_breakpoints] == [
        "after_system",
        "after_tools",
        "after_history_prefix",
    ]
    assert reused.epoch == legacy.epoch == 1
    assert reused.cache_breakpoints == fresh.cache_breakpoints == loaded.cache_breakpoints
    assert rotated.epoch == 2
    assert rotated.cache_breakpoints == loaded.cache_breakpoints
    # Breakpoints are provider markers, not prompt text: no prefix identity moves.
    assert {legacy.prefix_sha256, reused.prefix_sha256, fresh.prefix_sha256} == {
        legacy.prefix_sha256
    }
    assert reused.tool_schema_sha256 == legacy.tool_schema_sha256


def _marked(payload: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = [*payload["system"], *payload["tools"]]
    blocks.extend(block for message in payload["messages"] for block in message["content"])
    return [block for block in blocks if "cache_control" in block]


def _tool_loop_request() -> ModelRequest:
    reasoning = ProviderReasoningItem(
        item_index=0,
        provider="anthropic",
        provider_payload={"type": "thinking", "thinking": "", "signature": "sig"},
    )
    return ModelRequest(
        model_policy="contract",
        conversation=[
            SystemMessage(content=[TextPart(text="Platform.")]),
            UserMessage(content=[TextPart(text="Plan the trip.")]),
            AssistantMessage(content=[TextPart(text="Here is a plan.")]),
            UserMessage(content=[TextPart(text="Runtime metadata.")], trust=TrustLevel.PLATFORM),
            UserMessage(content=[TextPart(text="Now book it.")]),
            AssistantMessage(content=[TextPart(text="Opening the site.")]),
            reasoning,
            ToolCallItem(
                call_id="call-1",
                item_index=0,
                name="math.calculate",
                arguments={"expression": "1+1"},
                raw_arguments='{"expression":"1+1"}',
            ),
            ToolResultItem(call_id="call-1", content=[TextPart(text="2")]),
        ],
        tools=[CalculatorTool.spec],
        cache_hints=CacheHints(
            breakpoints=[
                CacheBreakpoint(boundary="after_system"),
                CacheBreakpoint(boundary="after_tools"),
                CacheBreakpoint(boundary="after_history_prefix", through_item=2),
                CacheBreakpoint(boundary="after_history_prefix", through_item=5),
            ]
        ),
    )


def _limited(maximum: int) -> ResolvedModel:
    return _claude().model_copy(
        update={"limits": ModelLimits(max_cache_breakpoints=maximum)}, deep=True
    )


def test_anthropic_places_each_history_marker_on_the_item_it_names() -> None:
    payload, sent, dropped = AnthropicMessagesProvider._request_payload(
        _tool_loop_request(), _claude()
    )

    marked = _marked(payload)
    assert (sent, dropped) == (4, 0)
    assert len(marked) == 4
    assert payload["system"][-1] in marked
    assert payload["tools"][-1] in marked
    assert {"type": "text", "text": "Here is a plan.", "cache_control": {"type": "ephemeral"}} in (
        marked
    )
    assert {
        "type": "text",
        "text": "Opening the site.",
        "cache_control": {"type": "ephemeral"},
    } in marked
    assert all(block.get("type") not in {"thinking", "tool_result"} for block in marked)


def test_anthropic_keeps_the_earliest_hints_a_model_budget_allows() -> None:
    three, sent, dropped = AnthropicMessagesProvider._request_payload(
        _tool_loop_request(), _limited(3)
    )
    assert (sent, dropped) == (3, 1)
    assert [block["text"] for block in _marked(three) if block.get("type") == "text"] == [
        "Platform.",
        "Here is a plan.",
    ]

    none, sent, dropped = AnthropicMessagesProvider._request_payload(
        _tool_loop_request(), _limited(0)
    )
    assert (sent, dropped) == (0, 4)
    assert _marked(none) == []


def test_an_unplaced_history_hint_still_marks_the_final_block() -> None:
    request = _tool_loop_request()
    request.cache_hints = CacheHints(breakpoints=[CacheBreakpoint(boundary="after_history_prefix")])

    payload, _, _ = AnthropicMessagesProvider._request_payload(request, _claude())

    assert _marked(payload) == [payload["messages"][-1]["content"][-1]]
