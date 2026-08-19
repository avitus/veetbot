"""Perform one approved, revision-bound browser interaction."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from agent_core.domain.browser import BrowserAction, BrowserProviderError
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import ToolExecutionContext, ToolFailureKind, ToolResult, ToolSpec
from agent_core.ports.browser import BrowserProvider, bind_browser_execution
from agent_core.tools.browser_navigate import (
    OUTPUT_SCHEMA,
    _failure,
    _observation_result,
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["click", "type", "select", "check", "press", "scroll"],
        },
        "expected_revision": {"type": "string", "minLength": 1, "maxLength": 128},
        "ref": {"type": "string", "minLength": 1, "maxLength": 128},
        "value": {"type": "string", "maxLength": 4096},
        "key": {
            "type": "string",
            "enum": [
                "Enter",
                "Escape",
                "Tab",
                "Space",
                "ArrowUp",
                "ArrowDown",
                "ArrowLeft",
                "ArrowRight",
            ],
        },
        "delta_y": {"type": "integer", "minimum": -2000, "maximum": 2000},
    },
    "required": ["kind", "expected_revision", "ref"],
    "additionalProperties": False,
}


def _action_failure(error: BrowserProviderError) -> ToolResult:
    if error.reason_code == "tool.browser.outcome_unknown":
        kind = ToolFailureKind.OUTCOME_UNKNOWN
    elif error.reason_code == "tool.browser.element_not_found":
        kind = ToolFailureKind.NOT_FOUND
    elif error.reason_code in {
        "tool.browser.page_changed",
        "tool.browser.action_not_allowed",
    }:
        kind = ToolFailureKind.INVALID_ARGUMENTS
    elif error.reason_code in {
        "tool.browser.profile_unavailable",
        "tool.browser.authentication_required",
        "tool.browser.needs_user",
    }:
        kind = ToolFailureKind.PERMISSION
    elif error.reason_code == "tool.browser.output_invalid":
        kind = ToolFailureKind.OUTPUT_INVALID
    elif error.reason_code == "tool.browser.provider_unavailable":
        kind = ToolFailureKind.TRANSPORT
    else:
        kind = ToolFailureKind.UPSTREAM_ERROR
    return _failure(kind, error.reason_code, retryable=error.retryable)


class BrowserActTool:
    spec = ToolSpec(
        name="browser.act",
        version="1.0.0",
        description="Perform one approved action on an element from the current page revision.",
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        side_effect=SideEffectClass.EXTERNAL_WRITE,
        risk=RiskLevel.HIGH,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        timeout_seconds=30,
        maximum_output_bytes=512 * 1024,
        allow_parallel=False,
        target_kind="browser_provider",
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )

    def __init__(self, provider: BrowserProvider) -> None:
        self._provider = provider

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        try:
            action = BrowserAction.model_validate(arguments)
        except ValidationError:
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.arguments_invalid",
                retryable=False,
            )
        try:
            await bind_browser_execution(self._provider, context)
            await context.mark_effect_sent()
            observation = await self._provider.act(action)
        except BrowserProviderError as error:
            return _action_failure(error)
        return _observation_result(self._provider, observation)
