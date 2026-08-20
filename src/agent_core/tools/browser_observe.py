"""Observe the current page in a trusted browser provider."""

from __future__ import annotations

from typing import Any

from agent_core.domain.browser import BrowserProviderError
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
    "properties": {},
    "additionalProperties": False,
}


class BrowserObserveTool:
    spec = ToolSpec(
        name="browser.observe",
        version="1.0.0",
        description="Observe the current rendered page in the bound browser profile.",
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        side_effect=SideEffectClass.NETWORK_READ,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        timeout_seconds=30,
        maximum_output_bytes=512 * 1024,
        allow_parallel=False,
        target_kind="browser_provider",
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )

    def __init__(self, provider: BrowserProvider) -> None:
        self._provider = provider

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        if arguments:
            return browser_failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.arguments_invalid",
                retryable=False,
            )
        try:
            await bind_browser_execution(self._provider, context)
            observation = await self._provider.observe()
        except BrowserProviderError as error:
            return browser_failure(error)
        return observation_result(self._provider, observation, self.spec.maximum_output_bytes)
