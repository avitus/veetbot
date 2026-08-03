"""Fixed model-facing messages keyed only by stable reason code."""

from __future__ import annotations

TOOL_MESSAGES: dict[str, str] = {
    "tool.succeeded": "The tool completed successfully.",
    "tool.invalid_arguments.syntax": "The expression could not be parsed.",
    "tool.invalid_arguments.unknown_name": (
        "Unknown function or constant. Functions: abs, ceil, exp, floor, ln, "
        "log10, max, min, round, sqrt. Constants: pi, e."
    ),
    "tool.invalid_arguments.arity": "Wrong number of arguments for that function.",
    "tool.invalid_arguments.domain": "An argument is outside the function's domain.",
    "tool.invalid_arguments.division_by_zero": "Division by zero.",
    "tool.invalid_arguments.result_out_of_range": (
        "The result is too large or too small to represent."
    ),
    "tool.invalid_arguments.expression_too_long": (
        "The expression is longer than 1024 characters."
    ),
    "tool.invalid_arguments.expression_too_deep": (
        "The expression is nested more than 32 levels deep."
    ),
    "tool.invalid_arguments.unknown_timezone": (
        "Unknown timezone. Provide an IANA name such as UTC, America/New_York, or Europe/London."
    ),
    "tool.arguments_invalid": "The tool arguments did not match the declared schema.",
    "tool.output_invalid": "The tool returned data outside its declared contract.",
    "tool.timeout": "The tool did not finish within its allowed time.",
    "tool.internal_error": "The tool could not complete because of an internal error.",
    "tool.outcome_unknown": "The outcome of this call is unknown. Do not repeat it.",
    "policy.scope.missing": "Not performed. The principal lacks a required scope.",
    "policy.milestone1.non_pure": (
        "Not performed. This milestone authorizes only side-effect-free tools."
    ),
    "policy.matrix.unknown_tool": "Not performed. The requested capability is unknown.",
}


def message_for(reason_code: str) -> str:
    """Return narration written by the platform, never by an external system."""

    return TOOL_MESSAGES.get(
        reason_code, "The tool could not complete for a platform-defined reason."
    )
