"""Observe the current page in a trusted browser provider."""

from __future__ import annotations

from typing import Any

from agent_core.domain.browser import BrowserProviderError
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import ToolExecutionContext, ToolFailureKind, ToolResult, ToolSpec
from agent_core.ports.browser import BrowserProvider, bind_browser_execution
from agent_core.tools.browser_observation_pages import PAGE_SCHEMA, observation_page_result
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


class LegacyBrowserObserveTool:
    """Retain the original observation schema for chats pinned to version 1.0.0."""

    spec = ToolSpec(
        name="browser.observe",
        version="1.0.0",
        description=(
            "Read the current page of this chat's website profile again. navigate and act "
            "already return the settled page, so observe only to refresh a page that changes "
            "on its own."
        ),
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
        """Bind the provider used by the original observation contract."""
        self._provider = provider

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        """Read a full bounded observation without accepting pagination arguments."""
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


class BrowserObserveTool:
    """Expose complete observation pages that fit the configured inline output budget."""

    spec = LegacyBrowserObserveTool.spec.model_copy(
        update={
            "version": "1.1.0",
            "description": (
                "Read the current page without navigating, including controls missing from a "
                "truncated navigate/act/upload result. Returns complete JSON in bounded pages. "
                "Follow next_element_offset or next_text_offset using element_offset or "
                "text_offset; omit offsets to start at zero. For text alone set element_offset "
                "to total_elements. Each read refreshes the page: use only the latest revision "
                "and refs. Captured output artifacts are not workspace files."
                " After navigation_cancelled, observe to continue on the existing page."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "element_offset": {"type": "integer", "minimum": 0, "maximum": 256},
                    "text_offset": {"type": "integer", "minimum": 0, "maximum": 262144},
                },
                "additionalProperties": False,
            },
            "output_schema": PAGE_SCHEMA,
        }
    )

    def __init__(self, provider: BrowserProvider, *, inline_output_bytes: int = 4096) -> None:
        """Bind the provider and cap each page at the configured admission limit."""
        if inline_output_bytes < 1024:
            raise ValueError("browser observation pages require at least 1024 bytes")
        self._provider = provider
        self._inline_output_bytes = min(inline_output_bytes, self.spec.maximum_output_bytes)

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        """Validate offsets before binding, then return a page from a fresh observation."""
        bounds = {"element_offset": 256, "text_offset": 262144}
        if any(
            key not in bounds or type(value) is not int or not 0 <= value <= bounds[key]
            for key, value in arguments.items()
        ):
            return browser_failure(
                ToolFailureKind.INVALID_ARGUMENTS, "tool.arguments_invalid", retryable=False
            )
        try:
            await bind_browser_execution(self._provider, context)
            observation = await self._provider.observe()
        except BrowserProviderError as error:
            return browser_failure(error)
        return observation_page_result(
            self._provider,
            observation,
            self._inline_output_bytes,
            element_offset=arguments.get("element_offset", 0),
            text_offset=arguments.get("text_offset", 0),
        )
