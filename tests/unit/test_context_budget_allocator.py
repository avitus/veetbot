"""The context budget allocator: absolute caps, a scaling history, plan-time overflow.

context-engine.md ("Only history scales with the window") fixes the rules these
tests hold the allocator to: the output reserve is subtracted first, every class
except history is capped absolutely, and a window that cannot give history its
floor fails at plan time instead of producing a request that cannot answer.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_core.context.budget import ContextBudgetAllocator
from agent_core.domain.errors import ContextOverflow
from agent_core.domain.messages import ModelLimits, ResolvedModel

PLAN = Path(__file__).resolve().parents[2] / "src/agent_core/context/plan.yaml"
NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _config() -> dict[str, Any]:
    loaded = yaml.safe_load(PLAN.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _model(
    window: int = 32_768, *, max_output: int = 4_096, default_reserve: int = 4_096
) -> ResolvedModel:
    return ResolvedModel(
        provider="fake",
        model="scripted",
        limits=ModelLimits(
            context_window_tokens=window,
            max_output_tokens=max_output,
            default_output_reserve=default_reserve,
        ),
        resolved_at=NOW,
    )


def _bare(floor: int) -> dict[str, Any]:
    """The shipped plan with every proportional term removed, so history is the body."""

    config = _config()
    config["classes"]["tool_results"]["max_body_ratio"] = 0
    config["classes"]["working_state"]["max_tokens"] = 0
    config["classes"]["history"]["floor_tokens"] = floor
    config["estimator"]["safety_margin_ratio"] = 0
    return config


def test_shipped_plan_allocates_the_documented_caps_on_the_default_window() -> None:
    budget = ContextBudgetAllocator(_config()).allocate(_model(), prefix_tokens=10_000)

    body = 32_768 - 4_096 - 10_000
    usable = body - int(body * 0.05)
    tool_results = int(usable * 0.25)
    assert budget.total_tokens == 32_768
    assert budget.reserve_output_tokens == 4_096
    assert budget.platform_tokens == 2_000
    assert budget.agent_tokens == 4_000
    assert budget.persona_tokens == 2_000
    assert budget.tool_tokens == 9_000
    assert budget.skill_catalog_tokens == 1_500
    assert budget.skill_body_tokens == 6_000
    assert budget.retrieved_context_tokens == 1_500 + 2_000
    assert budget.working_state_tokens == 1_000
    assert budget.knowledge_tokens == 3_000
    assert budget.tool_result_tokens == tool_results
    assert budget.history_tokens == usable - 1_000 - tool_results
    assert budget.history_tokens >= 8_000
    assert budget.safety_margin_ratio == pytest.approx(0.05)


def test_only_history_and_tool_results_grow_with_the_window() -> None:
    allocator = ContextBudgetAllocator(_config())
    small = allocator.allocate(_model(32_768), prefix_tokens=10_000)
    large = allocator.allocate(_model(200_000), prefix_tokens=10_000)

    fixed = (
        "reserve_output_tokens",
        "platform_tokens",
        "agent_tokens",
        "persona_tokens",
        "tool_tokens",
        "skill_catalog_tokens",
        "skill_body_tokens",
        "retrieved_context_tokens",
        "working_state_tokens",
        "knowledge_tokens",
    )
    assert {name: getattr(large, name) for name in fixed} == {
        name: getattr(small, name) for name in fixed
    }
    assert large.history_tokens > small.history_tokens
    assert large.tool_result_tokens > small.tool_result_tokens


@pytest.mark.parametrize(
    ("max_output", "default_reserve", "reserve"),
    [(4_096, 4_096, 4_096), (16_384, 16_384, 8_192), (16_384, 2_048, 2_048)],
)
def test_output_reserve_is_the_smallest_of_policy_model_default_and_model_ceiling(
    max_output: int, default_reserve: int, reserve: int
) -> None:
    model = _model(128_000, max_output=max_output, default_reserve=default_reserve)

    budget = ContextBudgetAllocator(_config()).allocate(model, prefix_tokens=10_000)

    assert budget.reserve_output_tokens == reserve
    assert budget.input_capacity == 128_000 - reserve


def test_a_prefix_that_leaves_no_body_fails_at_plan_time() -> None:
    allocator = ContextBudgetAllocator(_config())

    with pytest.raises(ContextOverflow, match="exhaust the model window"):
        allocator.allocate(_model(), prefix_tokens=32_768 - 4_096)
    with pytest.raises(ContextOverflow, match="exhaust the model window"):
        allocator.allocate(_model(), prefix_tokens=40_000)


def test_history_floor_is_a_hard_boundary() -> None:
    allocator = ContextBudgetAllocator(_bare(floor=8_000))
    exact_prefix = 32_768 - 4_096 - 8_000

    assert allocator.allocate(_model(), prefix_tokens=exact_prefix).history_tokens == 8_000
    with pytest.raises(ContextOverflow, match="history floor"):
        allocator.allocate(_model(), prefix_tokens=exact_prefix + 1)


def test_shipped_plan_refuses_a_window_too_small_for_the_history_floor() -> None:
    with pytest.raises(ContextOverflow, match="history floor"):
        ContextBudgetAllocator(_config()).allocate(_model(16_384), prefix_tokens=2_000)


def test_a_session_snapshot_allocation_replaces_the_configured_snapshot_cap() -> None:
    allocator = ContextBudgetAllocator(_config())

    assert (
        allocator.allocate(
            _model(), prefix_tokens=10_000, memory_snapshot_tokens=0
        ).retrieved_context_tokens
        == 2_000
    )
    assert (
        allocator.allocate(
            _model(), prefix_tokens=10_000, memory_snapshot_tokens=700
        ).retrieved_context_tokens
        == 2_700
    )
    with pytest.raises(ValueError, match="snapshot token allocation must be nonnegative"):
        allocator.allocate(_model(), prefix_tokens=10_000, memory_snapshot_tokens=-1)


def test_optional_persona_and_snapshot_classes_default_to_zero() -> None:
    config = _config()
    del config["classes"]["persona"]
    del config["classes"]["memory_snapshot"]

    budget = ContextBudgetAllocator(config).allocate(_model(), prefix_tokens=10_000)

    assert budget.persona_tokens == 0
    assert budget.retrieved_context_tokens == 2_000


def _with(path: tuple[str, ...], value: object) -> dict[str, Any]:
    config = copy.deepcopy(_config())
    target = config
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return config


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("classes",), [], "classes must be a mapping"),
        (("output",), None, "output must be a mapping"),
        (("classes", "history"), 8_000, "classes.history must be a mapping"),
        (("classes", "persona"), 2_000, "classes.persona must be a mapping"),
        (("output", "reserve_tokens"), -1, "reserve_tokens must be a nonnegative integer"),
        (("output", "reserve_tokens"), True, "reserve_tokens must be a nonnegative integer"),
        (("classes", "agent_instructions", "max_tokens"), 4_000.5, "max_tokens must be"),
        (("classes", "history", "floor_tokens"), "8000", "floor_tokens must be"),
        (("classes", "tool_results", "max_body_ratio"), 1, "tool-result body ratio"),
        (("classes", "tool_results", "max_body_ratio"), -0.1, "tool-result body ratio"),
        (("classes", "tool_results", "max_body_ratio"), False, "tool-result body ratio"),
        (("estimator", "safety_margin_ratio"), 1.0, "safety margin"),
        (("estimator", "safety_margin_ratio"), True, "safety margin"),
        (("estimator", "safety_margin_ratio"), None, "safety margin"),
    ],
)
def test_malformed_budget_configuration_is_refused(
    path: tuple[str, ...], value: object, message: str
) -> None:
    allocator = ContextBudgetAllocator(_with(path, value))

    with pytest.raises(ValueError, match=message):
        allocator.allocate(_model(), prefix_tokens=10_000)
