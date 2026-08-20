"""Perform one approved, revision-bound browser interaction."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from agent_core.domain.browser import BrowserAction, BrowserProviderError
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import ToolExecutionContext, ToolFailureKind, ToolResult, ToolSpec
from agent_core.ports.browser import BrowserProvider, bind_browser_execution
from agent_core.tools.browser_results import (
    OUTPUT_SCHEMA,
    browser_failure,
    observation_result,
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
            return browser_failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.arguments_invalid",
                retryable=False,
            )
        try:
            await bind_browser_execution(self._provider, context)
            await context.mark_effect_sent()
            observation = await self._provider.act(action)
        except BrowserProviderError as error:
            return browser_failure(error)
        return observation_result(self._provider, observation, self.spec.maximum_output_bytes)
