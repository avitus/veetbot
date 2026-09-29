"""`math.calculate` grammar, functions, domains, and rendering (builtin-tools.md).

The Milestone 1 gates pin the bounds and the integer `//`/`%` differential; these pin
the rest of the closed grammar the design prints: precedence and associativity, the
function table with its domains, the error vocabulary, and the rendering rule.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from agent_core.domain.tools import ToolFailureKind
from agent_core.tools.calculator import CalculatorError, CalculatorTool, calculate
from tests.contract.support import tool_context


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        # ^ and ** are one right-associative operator; unary minus sits above power.
        ("-2^2", "-4"),
        ("-2**2", "-4"),
        ("2^3^2", "512"),
        ("2**3**2", "512"),
        ("(2^3)^2", "64"),
        ("2^-1", "0.5"),
        ("2 + 3 * 4", "14"),
        ("(2 + 3) * 4", "20"),
        ("10 - 4 - 3", "3"),
        ("100 / 10 / 5", "2"),
        ("2 * 3 % 4", "2"),
        ("7 // 2 * 2", "6"),
        ("--3", "3"),
        ("-+3", "-3"),
        ("  1\t+\n2  ", "3"),
        ("1.5e1 + 2E-1", "15.2"),
    ],
)
def test_precedence_and_associativity_follow_the_printed_grammar(
    expression: str, expected: str
) -> None:
    assert calculate(expression) == (expected, True)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        # Floor semantics hold for non-integers too; the remainder's sign follows the divisor.
        ("-7.5 // 2", "-4"),
        ("-7.5 % 2", "0.5"),
        ("7.5 // -2", "-4"),
        ("7.5 % -2", "-0.5"),
        ("7.5 // 2", "3"),
        ("7.5 % 2", "1.5"),
    ],
)
def test_floor_division_and_modulus_floor_for_fractions(expression: str, expected: str) -> None:
    assert calculate(expression) == (expected, True)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("abs(-3.5)", "3.5"),
        ("ceil(-1.5)", "-1"),
        ("floor(-1.5)", "-2"),
        ("ceil(1.2)", "2"),
        ("floor(1.8)", "1"),
        # round uses half-even, stated in the tool description.
        ("round(2.5)", "2"),
        ("round(3.5)", "4"),
        ("round(-2.5)", "-2"),
        ("sqrt(16)", "4"),
        ("sqrt(0)", "0"),
        ("log10(1000)", "3"),
        ("ln(1)", "0"),
        ("exp(0)", "1"),
        ("min(3, 1, 2)", "1"),
        ("max(3, 1, 2)", "3"),
        ("min(4)", "4"),
        ("max(abs(-9), sqrt(64))", "9"),
    ],
)
def test_function_table_values(expression: str, expected: str) -> None:
    assert calculate(expression) == (expected, True)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("round(1.25, 1)", "1.2"),
        ("round(1.35, 1)", "1.4"),
        ("round(-1.25, 1)", "-1.2"),
        ("round(1234, -2)", "1200"),
        ("round(1250, -2)", "1200"),
        ("round(3, 2)", "3"),
    ],
)
def test_round_to_places_is_half_even(expression: str, expected: str) -> None:
    assert calculate(expression)[0] == expected


def test_constants_are_rounded_to_context_precision_and_reported_inexact() -> None:
    pi, pi_exact = calculate("pi")
    e, e_exact = calculate("e")

    assert pi == "3.1415926535897932384626433832795028841971693993751"
    assert Decimal(e) == Decimal("2.718281828459045235360287471352662497757247093700")
    assert (pi_exact, e_exact) == (False, False)


@pytest.mark.parametrize(
    ("expression", "expected", "exact"),
    [
        ("1/3", "0." + "3" * 50, False),
        ("2/3", "0." + "6" * 49 + "7", False),
        ("1/4", "0.25", True),
        ("2.50 * 2", "5", True),
        ("0 * 1.5", "0", True),
        # Inside the -25..25 adjusted-exponent band the rendering is positional.
        ("1e25", "1" + "0" * 25, True),
        ("1e-25", "0." + "0" * 24 + "1", True),
        # Outside it the rendering is scientific with a lowercase exponent.
        ("1e-26", "1e-26", True),
        ("-1.5e30", "-1.5e+30", True),
    ],
)
def test_rendering_and_exactness(expression: str, expected: str, exact: bool) -> None:
    assert calculate(expression) == (expected, exact)


@pytest.mark.parametrize(
    ("expression", "reason"),
    [
        # Nothing outside the grammar is accepted.
        ("x = 1", "syntax"),
        ("1 < 2", "syntax"),
        ("'1'", "syntax"),
        ("[1]", "syntax"),
        ("math.pi", "syntax"),
        ("1_000", "syntax"),
        (".5", "syntax"),
        ("1.", "syntax"),
        ("1e", "syntax"),
        ("1e+", "syntax"),
        ("0x10", "syntax"),
        ("PI", "syntax"),
        ("1 +", "syntax"),
        ("(1", "syntax"),
        ("1)", "syntax"),
        ("2 3", "syntax"),
        ("", "syntax"),
        ("min(1,)", "syntax"),
        # Names are either in the fixed table or unknown; there is no trigonometry.
        # A function name not followed by "(" resolves as a constant.
        ("abs 1", "unknown_name"),
        ("tau", "unknown_name"),
        ("sin(1)", "unknown_name"),
        ("eval(1)", "unknown_name"),
        ("pi(1)", "unknown_name"),
        # Arity.
        ("min()", "arity"),
        ("abs()", "arity"),
        ("abs(1, 2)", "arity"),
        ("sqrt(4, 2)", "arity"),
        ("round(1, 2, 3)", "arity"),
        # Domains.
        ("sqrt(-1)", "domain"),
        ("ln(0)", "domain"),
        ("ln(-1)", "domain"),
        ("log10(0)", "domain"),
        ("round(1, 0.5)", "domain"),
        ("round(1, 51)", "domain"),
        ("round(1, -51)", "domain"),
        ("(-8)^(1/3)", "domain"),
        # Every divisor form fails the same way.
        ("1 / 0", "division_by_zero"),
        ("1 // 0", "division_by_zero"),
        ("1 % 0", "division_by_zero"),
        # A zero base under a negative exponent is a division by zero, not Infinity.
        ("0^-1", "division_by_zero"),
        ("0.0**-2", "division_by_zero"),
        # The magnitude bound is enforced by the context in both directions.
        ("1e9999 * 1e9999", "result_out_of_range"),
        ("1e-9999 * 1e-9999", "result_out_of_range"),
        ("exp(100000)", "result_out_of_range"),
    ],
)
def test_error_vocabulary(expression: str, reason: str) -> None:
    with pytest.raises(CalculatorError) as caught:
        calculate(expression)

    assert caught.value.reason == reason


def test_the_token_bound_is_512() -> None:
    at_bound = "+".join(["1"] * 256)  # 511 tokens before the end marker
    over_bound = "+".join(["1"] * 257)  # 513 tokens

    assert calculate(at_bound) == ("256", True)
    with pytest.raises(CalculatorError) as caught:
        calculate(over_bound)
    assert caught.value.reason == "expression_too_long"


@pytest.mark.parametrize(
    ("allowed", "refused"),
    [
        ("(" * 32 + "1" + ")" * 32, "(" * 33 + "1" + ")" * 33),
        ("-" * 32 + "1", "-" * 33 + "1"),
        ("abs(" * 32 + "1" + ")" * 32, "abs(" * 33 + "1" + ")" * 33),
    ],
    ids=["parentheses", "unary-chain", "calls"],
)
def test_the_depth_bound_counts_grammar_recursion(allowed: str, refused: str) -> None:
    assert calculate(allowed) == ("1", True)
    with pytest.raises(CalculatorError) as caught:
        calculate(refused)
    assert caught.value.reason == "expression_too_deep"


async def test_success_reports_the_value_exactness_and_expression() -> None:
    tool = CalculatorTool()

    exact = await tool.execute({"expression": "1/4"}, tool_context())
    inexact = await tool.execute({"expression": "1/3"}, tool_context())

    assert exact.ok and exact.structured == {
        "result": "0.25",
        "result_exact": True,
        "expression": "1/4",
    }
    assert [part.text for part in exact.content] == ["0.25"]  # type: ignore[union-attr]
    assert inexact.ok and inexact.structured is not None
    assert inexact.structured["result_exact"] is False
    assert inexact.content[0].text.endswith(  # type: ignore[union-attr]
        "\nrounded to 50 significant digits"
    )


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        ({"expression": "sqrt(-1)"}, "domain"),
        ({"expression": "1 +"}, "syntax"),
        ({"expression": "1 // 0"}, "division_by_zero"),
        ({"expression": 17}, "syntax"),
        ({}, "syntax"),
    ],
)
async def test_every_failure_is_non_retryable_invalid_arguments(
    arguments: dict[str, object], reason: str
) -> None:
    result = await CalculatorTool().execute(arguments, tool_context())

    assert result.ok is False
    assert result.content == []
    assert result.failure is not None
    assert result.failure.kind is ToolFailureKind.INVALID_ARGUMENTS
    assert result.failure.reason_code == f"tool.invalid_arguments.{reason}"
    assert result.failure.retryable is False
