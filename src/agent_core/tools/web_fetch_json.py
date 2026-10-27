"""Field selection from one public JSON document (ADR-0256).

The document is read through the same provider route as ``web.fetch``, so the
tool adds no egress of its own: the worker never connects to the model's URL.
Only the named fields reach the model, which keeps an API answer of many
kilobytes from filling the tool-result budget.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
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
        "fields": {
            "type": "array",
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "pattern": r"^[^.]+(\.[^.]+)*$",
            },
            "minItems": 1,
            "maxItems": 32,
            "uniqueItems": True,
        },
    },
    "required": ["url", "fields"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "provider": {"type": "string"},
        "url": {"type": "string"},
        "values": {"type": "object"},
        "missing": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["provider", "url", "values", "missing"],
    "additionalProperties": False,
}
_MISSING = object()


def _document(content: str) -> Any:
    """Parse the provider's text, unwrapping one Markdown code fence if present.

    Some providers return a JSON answer as it is and others as a fenced block
    in their Markdown; nothing else about the text is interpreted.
    """

    text = content.strip()
    if text.startswith("```"):
        first, _, rest = text.partition("\n")
        if rest.rstrip().endswith("```") and first.strip("`").strip() in {"", "json"}:
            text = rest.rstrip()[:-3]
    return json.loads(text)


def _select(value: Any, segments: Sequence[str]) -> Any:
    if not segments:
        return value
    head, rest = segments[0], segments[1:]
    if isinstance(value, list):
        if head == "*":
            selected = [_select(element, rest) for element in value]
            return [None if item is _MISSING else item for item in selected]
        if head.isascii() and head.isdigit() and int(head) < len(value):
            return _select(value[int(head)], rest)
        return _MISSING
    if isinstance(value, dict) and head in value:
        return _select(value[head], rest)
    return _MISSING


class WebFetchJsonTool:
    spec = ToolSpec(
        name="web.fetch_json",
        version="1.0.0",
        # The first sentence is the index entry, and the index has little room
        # left in a chat bound to a website profile (ADR-0256).
        description=(
            "Fetch named fields from one public HTTPS JSON document. Each field is a "
            "dotted path of object keys, array indexes, and * for every element of an "
            "array, as in ahead_by, commits.0.sha or *.name. Use it instead of web.fetch "
            "for an API answer when a few of its values are all you need; a path that "
            "matches nothing is listed as missing."
        ),
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        side_effect=SideEffectClass.NETWORK_READ,
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        timeout_seconds=60,
        maximum_output_bytes=65_536,
        allow_parallel=True,
        target_kind="web_provider",
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )

    def __init__(self, provider: WebProvider | WebProviderRouter) -> None:
        self._provider = provider
        self._router = _coerce_web_provider_router(provider)

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        url = arguments.get("url")
        fields = arguments.get("fields")
        if not isinstance(url, str) or not is_public_https_url(url):
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.web.url_disallowed",
                retryable=False,
            )
        if not isinstance(fields, list) or not all(isinstance(field, str) for field in fields):
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS, "tool.arguments_invalid", retryable=False
            )
        provider = self._router.select(routing_key=f"web.fetch_json:{context.invocation_id}")
        try:
            async with asyncio.timeout(_provider_timeout_seconds(context.timeout_seconds)):
                page = await provider.fetch(url)
        except TimeoutError:
            return _provider_failure(
                WebProviderError("tool.web.provider_unavailable", retryable=True),
                provider_name=provider.name,
            )
        except WebProviderError as error:
            return _provider_failure(error, provider_name=provider.name)
        try:
            document = _document(page.content)
        except ValueError:
            return _failure(
                ToolFailureKind.UPSTREAM_ERROR,
                "tool.web.json_invalid",
                retryable=False,
                provider_name=provider.name,
            )
        values: dict[str, Any] = {}
        missing: list[str] = []
        for field in fields:
            selected = _select(document, field.split("."))
            if selected is _MISSING:
                missing.append(field)
            else:
                values[field] = selected
        answer = {"url": page.url, "values": values, "missing": missing}
        part = TextPart(
            text=f"Provider: {provider.name}\n\n"
            + json.dumps(answer, ensure_ascii=False, separators=(",", ":"))
        )
        if len(_rendered_parts_bytes([part])) > self.spec.maximum_output_bytes:
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.web.json_selection_too_large",
                retryable=False,
                provider_name=provider.name,
            )
        return ToolResult(
            ok=True,
            content=[part],
            structured={"provider": provider.name, **answer},
            output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        )
