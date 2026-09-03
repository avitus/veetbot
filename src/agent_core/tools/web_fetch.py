"""Provider-neutral web page extraction tool."""

from __future__ import annotations

import asyncio
from typing import Any

from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import ToolExecutionContext, ToolFailureKind, ToolResult, ToolSpec
from agent_core.domain.web import WebProviderError, is_public_https_url
from agent_core.ports.web import WebProvider, WebProviderRouter
from agent_core.tools.web_search import (
    _coerce_web_provider_router,
    _failure,
    _provider_failure,
    _provider_timeout_seconds,
    _rendered_parts_bytes,
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "minLength": 1, "maxLength": 4096},
    },
    "required": ["url"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "provider": {"type": "string"},
        "url": {"type": "string"},
        "title": {"type": ["string", "null"]},
        "content": {"type": "string"},
    },
    "required": ["provider", "url", "title", "content"],
    "additionalProperties": False,
}


def _bounded_content(prefix: str, content: str, maximum_bytes: int) -> tuple[str, TextPart]:
    """Keep the longest character prefix whose serialized part stays in budget."""

    low, high = 0, len(content)
    selected = ""
    part = TextPart(text=prefix)
    while low <= high:
        midpoint = (low + high) // 2
        candidate = content[:midpoint]
        candidate_part = TextPart(text=prefix + candidate)
        if len(_rendered_parts_bytes([candidate_part])) <= maximum_bytes:
            selected, part = candidate, candidate_part
            low = midpoint + 1
        else:
            high = midpoint - 1
    return selected, part


class WebFetchTool:
    spec = ToolSpec(
        name="web.fetch",
        version="1.0.0",
        description="Fetch readable content from one public HTTPS page.",
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        side_effect=SideEffectClass.NETWORK_READ,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        timeout_seconds=60,
        maximum_output_bytes=1_048_576,
        allow_parallel=True,
        target_kind="web_provider",
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )

    def __init__(self, provider: WebProvider | WebProviderRouter) -> None:
        self._provider = provider
        self._router = _coerce_web_provider_router(provider)

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        url = arguments.get("url")
        if not isinstance(url, str) or not is_public_https_url(url):
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.web.url_disallowed",
                retryable=False,
            )
        provider = self._router.select(routing_key=f"web.fetch:{context.invocation_id}")
        try:
            # Provider clients apply their own 30-second connect/read/write
            # limits. The larger tool budget allows a progressing extraction
            # to complete while retaining a hard total deadline.
            async with asyncio.timeout(_provider_timeout_seconds(context.timeout_seconds)):
                page = await provider.fetch(url)
        except TimeoutError:
            return _provider_failure(
                WebProviderError("tool.web.provider_unavailable", retryable=True),
                provider_name=provider.name,
            )
        except WebProviderError as error:
            return _provider_failure(error, provider_name=provider.name)
        attribution = f"Provider: {provider.name}\n\n"
        content, part = _bounded_content(
            attribution,
            page.content,
            self.spec.maximum_output_bytes,
        )
        structured = {
            "provider": provider.name,
            "url": page.url,
            "title": page.title,
            "content": content,
        }
        return ToolResult(
            ok=True,
            content=[part],
            structured=structured,
            output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        )
