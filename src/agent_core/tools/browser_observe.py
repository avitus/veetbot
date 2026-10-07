"""Observe the current page in a trusted browser provider."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from agent_core.domain.browser import (
    BrowserCondition,
    BrowserObservationExpansion,
    BrowserProviderError,
)
from agent_core.domain.browser_extraction import BrowserExtractionRequest
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import ToolExecutionContext, ToolFailureKind, ToolResult, ToolSpec
from agent_core.ports.browser import (
    BrowserProvider,
    bind_browser_execution,
    expand_browser_observation,
    extract_browser_observation,
)
from agent_core.tools.browser_conditions import browser_condition_schema, check_browser_condition
from agent_core.tools.browser_observation_pages import PAGE_SCHEMA, observation_page_result
from agent_core.tools.browser_results import (
    OUTPUT_SCHEMA,
    browser_failure,
    observation_result,
)

_EXTRACTION_SCHEMA = BrowserExtractionRequest.model_json_schema()
_EXTRACTION_DEFINITIONS = {"f": _EXTRACTION_SCHEMA.pop("$defs")["BrowserExtractionField"]}
_EXTRACTION_SCHEMA["properties"]["fields"]["items"] = {"$ref": "#/$defs/f"}
_EXTRACTION_SCHEMA.pop("title", None)
_EXTRACTION_DEFINITIONS["f"].pop("title", None)
_CONDITION_SCHEMA = browser_condition_schema()
_CONDITION_DEFINITIONS = _CONDITION_SCHEMA.pop("$defs")

INPUT_SCHEMA: dict[str, Any] = {
    "$defs": {**_EXTRACTION_DEFINITIONS, **_CONDITION_DEFINITIONS},
    "type": "object",
    "properties": {
        "wait_for": _CONDITION_SCHEMA,
        "extract": _EXTRACTION_SCHEMA,
        **{
            name: {"type": "string", "minLength": minimum, "maxLength": 128}
            for name, minimum in (
                ("after", 1),
                ("cursor", 32),
                ("region_ref", 1),
                ("expected_revision", 1),
            )
        },
        "text_offset": {"type": "integer", "minimum": 0, "maximum": 262144},
    },
    "oneOf": [
        {
            "maxProperties": 1,
            "not": {
                "anyOf": [
                    {"required": [key]}
                    for key in ("region_ref", "expected_revision", "text_offset")
                ]
            },
        },
        {
            "required": ["region_ref", "expected_revision"],
            "properties": dict.fromkeys(("wait_for", "extract", "after", "cursor"), False),
        },
    ],
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
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
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


class PagedBrowserObserveTool:
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


class BrowserObserveTool:
    spec = ToolSpec(
        name="browser.observe",
        version="1.7.0",
        description=(
            "Read modes: {}; next_observe "
            "after/cursor; region_ref+expected_revision with text_offset; extract "
            "table/list/form; wait_for role/name or evidence. Evidence: text=complete line, "
            "region=kind/text, location=exact url, row=collection/index/typed fields/value. "
            "timeout_ms: 2000 default, 5000 max. failure_evidence: text/region/location. Form "
            "columns: label/role/disabled/checked/required, never values. Checks are window-scoped."
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
        self._provider = provider

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        try:
            condition = None
            expansion = None
            extraction = None
            if "extract" in arguments:
                if set(arguments) != {"extract"}:
                    raise ValueError("extraction cannot be combined with another mode")
                extraction = BrowserExtractionRequest.model_validate(arguments["extract"])
            elif "wait_for" in arguments:
                if set(arguments) != {"wait_for"}:
                    raise ValueError("wait and expansion cannot be combined")
                condition = BrowserCondition.model_validate(arguments["wait_for"])
            elif arguments:
                expansion = BrowserObservationExpansion.model_validate(arguments)
        except (ValidationError, ValueError):
            return browser_failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.arguments_invalid",
                retryable=False,
            )
        try:
            await bind_browser_execution(self._provider, context)
            observation = (
                await extract_browser_observation(self._provider, extraction)
                if extraction is not None
                else (
                    await self._provider.observe()
                    if expansion is None
                    else await expand_browser_observation(self._provider, expansion)
                )
            )
            if condition is not None:
                observation = await check_browser_condition(self._provider, observation, condition)
        except BrowserProviderError as error:
            return browser_failure(error)
        return observation_result(self._provider, observation, self.spec.maximum_output_bytes)
