"""Milestone 1 builtin-tool hard gates."""

from __future__ import annotations

from decimal import Decimal
from time import perf_counter

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agent_core.adapters.determinism import FixedClock
from agent_core.domain.errors import ToolValidationError
from agent_core.tools.calculator import (
    CalculatorError,
    CalculatorTool,
    Parser,
    calculate,
    tokenize,
)
from agent_core.tools.current_time import CurrentTimeTool
from agent_core.tools.messages import TOOL_MESSAGES
from agent_core.tools.registry import validate_registration
from tests.contract.support import NOW, tool_context

CALCULATOR_REASONS = {
    "syntax",
    "unknown_name",
    "arity",
    "domain",
    "division_by_zero",
    "result_out_of_range",
    "expression_too_long",
    "expression_too_deep",
}


def test_registration() -> None:
    assert validate_registration(CalculatorTool().spec) == CalculatorTool().spec
    clock = FixedClock(NOW)
    assert validate_registration(CurrentTimeTool(clock).spec) == CurrentTimeTool(clock).spec


def test_reserved_domain() -> None:
    invalid = CalculatorTool().spec.model_copy(update={"name": "mcp.calculate"})
    with pytest.raises(ToolValidationError, match="reserved domains"):
        validate_registration(invalid)


def test_demonstration() -> None:
    rendered, exact = calculate("17 * 23")
    assert rendered.encode("utf-8") == b"391"
    assert exact is True
    assert calculate("2 * 0") == ("0", True)


@st.composite
def expressions(draw: st.DrawFn) -> str:
    atom = st.integers(min_value=-1000, max_value=1000).map(str)
    expression = st.recursive(
        atom,
        lambda child: st.tuples(
            child, st.sampled_from([" + ", " - ", " * ", " / ", " // ", " % "]), child
        ).map("".join),
        max_leaves=10,
    )
    return draw(expression)


@settings(max_examples=200, deadline=50)
@given(expression=expressions())
def test_parser_property(expression: str) -> None:
    try:
        value = Parser(tokenize(expression)).parse()
        rendered, exact = calculate(expression)
    except CalculatorError as exc:
        assert exc.reason in CALCULATOR_REASONS
    else:
        assert isinstance(value, Decimal)
        assert isinstance(rendered, str)
        assert isinstance(exact, bool)


@pytest.mark.parametrize(
    ("expression", "reason"),
    [
        ("9**9**9", "result_out_of_range"),
        ("9^9^9", "result_out_of_range"),
        ("1" * 1025, "expression_too_long"),
        ("(" * 33 + "1" + ")" * 33, "expression_too_deep"),
    ],
)
def test_bounds_latency(expression: str, reason: str) -> None:
    started = perf_counter()
    with pytest.raises(CalculatorError) as caught:
        calculate(expression)
    elapsed = perf_counter() - started
    assert caught.value.reason == reason
    assert elapsed < 0.05


@given(
    left=st.integers(min_value=-10_000, max_value=10_000),
    right=st.integers(min_value=-10_000, max_value=10_000).filter(bool),
)
def test_int_differential(left: int, right: int) -> None:
    quotient, quotient_exact = calculate(f"{left}//{right}")
    remainder, remainder_exact = calculate(f"{left}%{right}")
    assert Decimal(quotient) == left // right
    assert Decimal(remainder) == left % right
    assert quotient_exact and remainder_exact


def test_decimal_exact() -> None:
    assert calculate("0.1 + 0.2") == ("0.3", True)
    assert calculate("1e26") == ("1e+26", True)


@pytest.mark.asyncio
async def test_clock_stability() -> None:
    tool = CurrentTimeTool(FixedClock(NOW))
    first = await tool.execute({"timezone": "America/Los_Angeles"}, tool_context())
    second = await tool.execute({"timezone": "America/Los_Angeles"}, tool_context())
    assert first.model_dump_json() == second.model_dump_json()


def test_message_table() -> None:
    expected = {
        *(f"tool.invalid_arguments.{reason}" for reason in CALCULATOR_REASONS),
        "tool.invalid_arguments.unknown_timezone",
    }
    assert expected <= TOOL_MESSAGES.keys()
    assert all("{" not in message and "}" not in message for message in TOOL_MESSAGES.values())
