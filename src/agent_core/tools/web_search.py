"""Provider-neutral web search tool."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolResult,
    ToolSpec,
)
from agent_core.domain.web import WebProviderError, WebSearchRequest
from agent_core.ports.web import WebProvider

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 500},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        "include_domains": {
            "type": "array",
            "items": {"type": "string", "minLength": 3, "maxLength": 253},
            "maxItems": 10,
            "uniqueItems": True,
        },
        "exclude_domains": {
            "type": "array",
            "items": {"type": "string", "minLength": 3, "maxLength": 253},
            "maxItems": 10,
            "uniqueItems": True,
        },
        "recency": {"type": "string", "enum": ["day", "week", "month", "year"]},
    },
    "required": ["query"],
    "not": {"required": ["include_domains", "exclude_domains"]},
    "additionalProperties": False,
}
RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "url": {"type": "string"},
        "snippet": {"type": "string"},
    },
    "required": ["title", "url", "snippet"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "provider": {"type": "string"},
        "results": {"type": "array", "items": RESULT_SCHEMA, "maxItems": 10},
    },
    "required": ["provider", "results"],
    "additionalProperties": False,
}


def _failure(
    kind: ToolFailureKind,
    reason_code: str,
    *,
    retryable: bool,
) -> ToolResult:
    return ToolResult(
        ok=False,
        content=[],
        failure=ToolFailure(
            kind=kind,
            reason_code=reason_code,
            detail="web access failed at a platform-controlled boundary",
            retryable=retryable,
        ),
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )


def _provider_failure(error: WebProviderError) -> ToolResult:
    if error.reason_code == "tool.web.auth_failed":
        kind = ToolFailureKind.PERMISSION
    elif error.reason_code == "tool.web.output_invalid":
        kind = ToolFailureKind.OUTPUT_INVALID
    elif error.reason_code == "tool.web.provider_unavailable":
        kind = ToolFailureKind.TRANSPORT
    else:
        kind = ToolFailureKind.UPSTREAM_ERROR
    return _failure(kind, error.reason_code, retryable=error.retryable)


class WebSearchTool:
    spec = ToolSpec(
        name="web.search",
        version="1.0.0",
        description="Search the public web through the configured provider.",
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        side_effect=SideEffectClass.NETWORK_READ,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        timeout_seconds=30,
        maximum_output_bytes=128 * 1024,
        allow_parallel=True,
        target_kind="web_provider",
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )

    def __init__(self, provider: WebProvider) -> None:
        self._provider = provider

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        del context
        try:
            request = WebSearchRequest.model_validate(arguments)
        except ValidationError:
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.arguments_invalid",
                retryable=False,
            )
        try:
            results = await self._provider.search(request)
        except WebProviderError as error:
            return _provider_failure(error)
        structured = {
            "provider": self._provider.name,
            "results": [result.model_dump(mode="json") for result in results],
        }
        return ToolResult(
            ok=True,
            content=[
                TextPart(
                    text=json.dumps(
                        structured,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
            ],
            structured=structured,
            output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        )
