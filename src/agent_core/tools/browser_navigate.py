"""Navigate a trusted browser provider to one public HTTPS page."""

from __future__ import annotations

from typing import Any

from agent_core.domain.browser import BrowserProviderError
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailureKind,
    ToolResult,
    ToolSpec,
)
from agent_core.domain.web import is_public_https_url
from agent_core.ports.browser import BrowserProvider, bind_browser_execution
from agent_core.tools.browser_results import (
    OUTPUT_SCHEMA,
    browser_failure,
    observation_result,
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"url": {"type": "string", "minLength": 1, "maxLength": 4096}},
    "required": ["url"],
    "additionalProperties": False,
}


class BrowserNavigateTool:
    spec = ToolSpec(
        name="browser.navigate",
        version="1.0.0",
        description="Navigate the bound browser profile to one allowed public HTTPS page.",
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
        url = arguments.get("url")
        if not isinstance(url, str) or set(arguments) != {"url"} or not is_public_https_url(url):
            return browser_failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.browser.url_disallowed",
                retryable=False,
            )
        try:
            await bind_browser_execution(self._provider, context)
            if not self._provider.allows(url):
                return browser_failure(
                    ToolFailureKind.INVALID_ARGUMENTS,
                    "tool.browser.url_disallowed",
                    retryable=False,
                )
            observation = await self._provider.navigate(url)
        except BrowserProviderError as error:
            return browser_failure(error)
        return observation_result(
            self._provider,
            observation,
            self.spec.maximum_output_bytes,
        )
