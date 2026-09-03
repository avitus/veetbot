"""Provider-neutral web search tool."""

from __future__ import annotations

import asyncio
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
from agent_core.domain.web import WebProviderError, WebSearchRequest, WebSearchResult
from agent_core.ports.web import WebProvider, WebProviderRouter

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
    provider_name: str | None = None,
) -> ToolResult:
    provider_data = (
        None
        if provider_name is None
        else json.dumps(
            {"provider": provider_name},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return ToolResult(
        ok=False,
        content=[],
        structured=None if provider_name is None else {"provider": provider_name},
        failure=ToolFailure(
            kind=kind,
            reason_code=reason_code,
            detail="web access failed at a platform-controlled boundary",
            retryable=retryable,
            external_text=provider_data,
        ),
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )


def _provider_failure(error: WebProviderError, *, provider_name: str) -> ToolResult:
    if error.reason_code == "tool.web.auth_failed":
        kind = ToolFailureKind.PERMISSION
    elif error.reason_code == "tool.web.output_invalid":
        kind = ToolFailureKind.OUTPUT_INVALID
    elif error.reason_code == "tool.web.provider_unavailable":
        kind = ToolFailureKind.TRANSPORT
    else:
        kind = ToolFailureKind.UPSTREAM_ERROR
    return _failure(
        kind,
        error.reason_code,
        retryable=error.retryable,
        provider_name=provider_name,
    )


def _provider_timeout_seconds(effective_timeout_seconds: float) -> float:
    margin = min(0.25, effective_timeout_seconds * 0.1)
    return max(0.0, effective_timeout_seconds - margin)


def _rendered_parts_bytes(parts: list[TextPart]) -> bytes:
    """Render parts exactly as the execution pipeline measures their output."""

    return json.dumps(
        [part.model_dump(mode="json") for part in parts],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


async def _provider_search(
    provider: WebProvider,
    request: WebSearchRequest,
    context: ToolExecutionContext,
) -> tuple[WebSearchResult, ...] | ToolResult:
    # Finish slightly before the pipeline's outer deadline so the selected
    # provider survives in the normalized failure instead of being replaced
    # by an unattributed generic tool timeout.
    timeout_seconds = _provider_timeout_seconds(context.timeout_seconds)
    try:
        async with asyncio.timeout(timeout_seconds):
            return await provider.search(request)
    except TimeoutError:
        return _provider_failure(
            WebProviderError("tool.web.provider_unavailable", retryable=True),
            provider_name=provider.name,
        )
    except WebProviderError as error:
        return _provider_failure(error, provider_name=provider.name)


class _FixedWebProviderRouter:
    def __init__(self, provider: WebProvider) -> None:
        self.allocations = ()
        self._provider = provider

    def select(self, *, routing_key: str) -> WebProvider:
        del routing_key
        return self._provider


def _coerce_web_provider_router(
    provider: WebProvider | WebProviderRouter,
) -> WebProviderRouter:
    return (
        provider if isinstance(provider, WebProviderRouter) else _FixedWebProviderRouter(provider)
    )


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

    def __init__(self, provider: WebProvider | WebProviderRouter) -> None:
        self._provider = provider
        self._router = _coerce_web_provider_router(provider)

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        try:
            request = WebSearchRequest.model_validate(arguments)
        except ValidationError:
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.arguments_invalid",
                retryable=False,
            )
        provider = self._router.select(routing_key=f"web.search:{context.invocation_id}")
        provider_result = await _provider_search(provider, request, context)
        if isinstance(provider_result, ToolResult):
            return provider_result
        results = provider_result
        records = [result.model_dump(mode="json") for result in results[: request.max_results]]
        # The declared output limit is a platform contract; a full page of
        # maximal results can exceed it, so drop trailing results until the
        # rendered part list fits. One schema-bounded result always fits.
        while True:
            structured: dict[str, Any] = {"provider": provider.name, "results": records}
            part = TextPart(
                text=json.dumps(
                    structured,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            rendered = _rendered_parts_bytes([part])
            if len(rendered) <= self.spec.maximum_output_bytes or not records:
                break
            records = records[:-1]
        return ToolResult(
            ok=True,
            content=[part],
            structured=structured,
            output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        )
