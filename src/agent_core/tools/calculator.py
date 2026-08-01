"""Closed-grammar Decimal calculator for the Milestone 1 vertical slice."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    Underflow,
    localcontext,
)
from typing import Any

from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolResult,
    ToolSpec,
)

MAX_EXPRESSION_LENGTH = 1024
MAX_TOKENS = 512
MAX_DEPTH = 32

CALCULATOR_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "expression": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_EXPRESSION_LENGTH,
            "description": "A mathematical expression, e.g. 17 * 23",
        }
    },
    "required": ["expression"],
    "additionalProperties": False,
}

CALCULATOR_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "result": {"type": "string"},
        "result_exact": {"type": "boolean"},
        "expression": {"type": "string"},
    },
    "required": ["result", "result_exact", "expression"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    value: str
    offset: int


class CalculatorError(ValueError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def tokenize(expression: str) -> list[Token]:
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise CalculatorError("expression_too_long", "expression exceeds 1024 characters")
    tokens: list[Token] = []
    index = 0
    while index < len(expression):
        character = expression[index]
        if character.isspace():
            index += 1
            continue
        start = index
        if character.isdigit():
            index += 1
            while index < len(expression) and expression[index].isdigit():
                index += 1
            if index < len(expression) and expression[index] == ".":
                index += 1
                decimal_start = index
                while index < len(expression) and expression[index].isdigit():
                    index += 1
                if index == decimal_start:
                    raise CalculatorError("syntax", f"missing digit at offset {index}")
            if index < len(expression) and expression[index] in {"e", "E"}:
                index += 1
                if index < len(expression) and expression[index] in {"+", "-"}:
                    index += 1
                exponent_start = index
                while index < len(expression) and expression[index].isdigit():
                    index += 1
                if index == exponent_start:
                    raise CalculatorError("syntax", f"missing exponent at offset {index}")
            tokens.append(Token("number", expression[start:index], start))
        elif character.isalpha() and character.islower():
            index += 1
            while index < len(expression) and (
                expression[index].islower()
                or expression[index].isdigit()
                or expression[index] == "_"
            ):
                index += 1
            tokens.append(Token("name", expression[start:index], start))
        elif expression.startswith("**", index) or expression.startswith("//", index):
            tokens.append(Token("operator", expression[index : index + 2], index))
            index += 2
        elif character in "+-*/%^(),":
            kind = "punctuation" if character in "()," else "operator"
            tokens.append(Token(kind, character, index))
            index += 1
        else:
            raise CalculatorError("syntax", f"unexpected token at offset {index}")
        if len(tokens) > MAX_TOKENS:
            raise CalculatorError("expression_too_long", "expression exceeds 512 tokens")
    tokens.append(Token("eof", "", len(expression)))
    return tokens


class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self._tokens = tokens
        self._index = 0

    @property
    def current(self) -> Token:
        return self._tokens[self._index]

    def _take(self, value: str | None = None) -> Token:
        token = self.current
        if value is not None and token.value != value:
            raise CalculatorError("syntax", f"expected {value!r} at offset {token.offset}")
        self._index += 1
        return token

    @staticmethod
    def _guard_depth(depth: int) -> None:
        if depth > MAX_DEPTH:
            raise CalculatorError("expression_too_deep", "expression exceeds depth 32")

    def parse(self) -> Decimal:
        value = self._expression(0)
        if self.current.kind != "eof":
            raise CalculatorError("syntax", f"unexpected token at offset {self.current.offset}")
        return value

    def _expression(self, depth: int) -> Decimal:
        value = self._term(depth)
        while self.current.value in {"+", "-"}:
            operator = self._take().value
            right = self._term(depth)
            value = value + right if operator == "+" else value - right
        return value

    def _term(self, depth: int) -> Decimal:
        value = self._unary(depth)
        while self.current.value in {"*", "/", "//", "%"}:
            operator = self._take().value
            right = self._unary(depth)
            if right == 0:
                raise CalculatorError("division_by_zero", "zero divisor")
            if operator == "*":
                value *= right
            elif operator == "/":
                value /= right
            else:
                # Decimal // truncates. Adjust an inexact negative quotient to Python floor
                # without performing a rounded division that would poison result_exact.
                quotient = value // right
                if (value < 0) != (right < 0) and value % right != 0:
                    quotient -= 1
                value = quotient if operator == "//" else value - right * quotient
        return value

    def _unary(self, depth: int) -> Decimal:
        if self.current.value in {"+", "-"}:
            self._guard_depth(depth + 1)
            operator = self._take().value
            value = self._unary(depth + 1)
            return value if operator == "+" else -value
        return self._power(depth)

    def _power(self, depth: int) -> Decimal:
        value = self._primary(depth)
        if self.current.value in {"^", "**"}:
            self._guard_depth(depth + 1)
            self._take()
            exponent = self._unary(depth + 1)
            value **= exponent
        return value

    def _primary(self, depth: int) -> Decimal:
        token = self.current
        if token.kind == "number":
            self._take()
            return Decimal(token.value)
        if token.kind == "name":
            name = self._take().value
            if self.current.value == "(":
                return self._call(name, depth + 1)
            constants = {
                "pi": Decimal("3.14159265358979323846264338327950288419716939937510582097494"),
                "e": Decimal("2.71828182845904523536028747135266249775724709369995957496696"),
            }
            try:
                return +constants[name]
            except KeyError as exc:
                raise CalculatorError("unknown_name", f"unknown name {name!r}") from exc
        if token.value == "(":
            self._guard_depth(depth + 1)
            self._take("(")
            value = self._expression(depth + 1)
            self._take(")")
            return value
        raise CalculatorError("syntax", f"expected a value at offset {token.offset}")

    def _call(self, name: str, depth: int) -> Decimal:
        self._guard_depth(depth)
        self._take("(")
        arguments: list[Decimal] = []
        if self.current.value != ")":
            while True:
                arguments.append(self._expression(depth))
                if self.current.value != ",":
                    break
                self._take(",")
        self._take(")")
        return evaluate_function(name, arguments)


def _require_arity(name: str, arguments: list[Decimal], minimum: int, maximum: int) -> None:
    if not minimum <= len(arguments) <= maximum:
        raise CalculatorError("arity", f"{name} received {len(arguments)} arguments")


def evaluate_function(name: str, arguments: list[Decimal]) -> Decimal:
    if name in {"min", "max"}:
        _require_arity(name, arguments, 1, MAX_TOKENS)
        return min(arguments) if name == "min" else max(arguments)
    if name == "round":
        _require_arity(name, arguments, 1, 2)
        if len(arguments) == 1:
            return arguments[0].to_integral_value(rounding=ROUND_HALF_EVEN)
        places = arguments[1]
        if places != places.to_integral_value() or not Decimal(-50) <= places <= Decimal(50):
            raise CalculatorError("domain", "round places must be an integer from -50 to 50")
        return arguments[0].quantize(Decimal(1).scaleb(-int(places)))
    _require_arity(name, arguments, 1, 1)
    value = arguments[0]
    if name == "abs":
        return abs(value)
    if name == "ceil":
        return value.to_integral_value(rounding=ROUND_CEILING)
    if name == "floor":
        return value.to_integral_value(rounding=ROUND_FLOOR)
    if name in {"sqrt", "ln", "log10"}:
        if (name == "sqrt" and value < 0) or (name != "sqrt" and value <= 0):
            raise CalculatorError("domain", f"{name} domain error")
        if name == "sqrt":
            return value.sqrt()
        if name == "ln":
            return value.ln()
        return value.log10()
    if name == "exp":
        return value.exp()
    raise CalculatorError("unknown_name", f"unknown function {name!r}")


def render_decimal(value: Decimal) -> str:
    if -25 <= value.adjusted() <= 25:
        rendered = format(value, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return rendered or "0"
    return format(value, "E").replace("E", "e")


def calculate(expression: str) -> tuple[str, bool]:
    try:
        with localcontext() as context:
            context.prec = 50
            context.rounding = ROUND_HALF_EVEN
            context.Emax = 10_000
            context.Emin = -10_000
            for signal in (InvalidOperation, DivisionByZero, Overflow, Underflow):
                context.traps[signal] = True
            context.clear_flags()
            value = Parser(tokenize(expression)).parse()
            exact = not context.flags[Inexact]
            return render_decimal(value), exact
    except CalculatorError:
        raise
    except DivisionByZero as exc:
        raise CalculatorError("division_by_zero", "zero divisor") from exc
    except (Overflow, Underflow) as exc:
        raise CalculatorError("result_out_of_range", "decimal magnitude bound exceeded") from exc
    except InvalidOperation as exc:
        raise CalculatorError("domain", "decimal operation outside its domain") from exc


class CalculatorTool:
    spec = ToolSpec(
        name="math.calculate",
        version="1.0.0",
        description=(
            "Evaluate a closed arithmetic grammar with 50-digit Decimal precision. "
            "The // and % operators use Python floor semantics; round uses half-even."
        ),
        input_schema=CALCULATOR_INPUT_SCHEMA,
        output_schema=CALCULATOR_OUTPUT_SCHEMA,
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        required_scopes=set(),
        timeout_seconds=2,
        maximum_output_bytes=4096,
        allow_parallel=True,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        del context
        expression = arguments.get("expression")
        if not isinstance(expression, str):
            error = CalculatorError("syntax", "expression was not a string")
        else:
            try:
                result, exact = calculate(expression)
            except CalculatorError as exc:
                error = exc
            else:
                content = result if exact else f"{result}\nrounded to 50 significant digits"
                return ToolResult(
                    ok=True,
                    content=[TextPart(text=content)],
                    structured={
                        "result": result,
                        "result_exact": exact,
                        "expression": expression,
                    },
                )
        return ToolResult(
            ok=False,
            content=[],
            failure=ToolFailure(
                kind=ToolFailureKind.INVALID_ARGUMENTS,
                reason_code=f"tool.invalid_arguments.{error.reason}",
                detail=error.detail,
                retryable=False,
            ),
        )
