"""Navigate a trusted browser provider to one public HTTPS page."""

from __future__ import annotations

import json
from typing import Any

from agent_core.domain.browser import BrowserObservation, BrowserProviderError
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolResult,
    ToolSpec,
)
from agent_core.domain.web import is_public_https_url
from agent_core.ports.browser import BrowserProvider, bind_browser_execution

MAX_TEXT_BYTES = 256 * 1024
MAX_PROVIDER_BYTES = 128
MAX_TITLE_BYTES = 4 * 1024
MAX_REVISION_BYTES = 512
INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"url": {"type": "string", "minLength": 1, "maxLength": 4096}},
    "required": ["url"],
    "additionalProperties": False,
}
ELEMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ref": {"type": "string"},
        "role": {"type": "string"},
        "name": {"type": "string"},
        "disabled": {"type": "boolean"},
        "checked": {"type": ["boolean", "null"]},
    },
    "required": ["ref", "role", "name", "disabled", "checked"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "provider": {"type": "string"},
        "url": {"type": "string"},
        "title": {"type": ["string", "null"]},
        "revision": {"type": "string"},
        "text": {"type": "string"},
        "elements": {"type": "array", "items": ELEMENT_SCHEMA, "maxItems": 256},
    },
    "required": ["provider", "url", "title", "revision", "text", "elements"],
    "additionalProperties": False,
}


def _failure(kind: ToolFailureKind, reason_code: str, *, retryable: bool) -> ToolResult:
    return ToolResult(
        ok=False,
        content=[],
        failure=ToolFailure(
            kind=kind,
            reason_code=reason_code,
            detail="browser access failed at a platform-controlled boundary",
            retryable=retryable,
        ),
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )


def _provider_failure(error: BrowserProviderError) -> ToolResult:
    if error.reason_code in {
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


def _bounded_utf8(value: str, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    return encoded[:maximum_bytes].decode("utf-8", errors="ignore")


def _serialized_observation(structured: dict[str, Any]) -> str:
    return json.dumps(
        structured,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _bounded_observation_payload(
    provider: BrowserProvider,
    observation: BrowserObservation,
    maximum_bytes: int,
) -> tuple[dict[str, Any], str]:
    elements = [element.model_dump(mode="json") for element in observation.elements]
    base: dict[str, Any] = {
        "provider": _bounded_utf8(provider.name, MAX_PROVIDER_BYTES),
        "url": observation.url,
        "title": (
            _bounded_utf8(observation.title, MAX_TITLE_BYTES)
            if observation.title is not None
            else None
        ),
        "revision": _bounded_utf8(observation.revision, MAX_REVISION_BYTES),
        "text": "",
        "elements": elements,
    }

    # Provider values are bounded by the domain, but JSON escaping can expand
    # them substantially. Preserve the largest leading element set that leaves
    # room, then use the remaining bytes for rendered text.
    if len(_serialized_observation(base).encode("utf-8")) > maximum_bytes:
        low = 0
        high = len(elements)
        while low < high:
            candidate_count = (low + high + 1) // 2
            base["elements"] = elements[:candidate_count]
            if len(_serialized_observation(base).encode("utf-8")) <= maximum_bytes:
                low = candidate_count
            else:
                high = candidate_count - 1
        base["elements"] = elements[:low]

    low = 0
    high = min(len(observation.text.encode("utf-8")), MAX_TEXT_BYTES)
    fitted = _serialized_observation(base)
    while low <= high:
        candidate_bytes = (low + high) // 2
        base["text"] = _bounded_utf8(observation.text, candidate_bytes)
        candidate = _serialized_observation(base)
        if len(candidate.encode("utf-8")) <= maximum_bytes:
            fitted = candidate
            low = candidate_bytes + 1
        else:
            high = candidate_bytes - 1
    base["text"] = _bounded_utf8(observation.text, high)
    return base, fitted


def _observation_result(provider: BrowserProvider, observation: BrowserObservation) -> ToolResult:
    if not provider.allows(observation.url):
        return _failure(
            ToolFailureKind.OUTPUT_INVALID,
            "tool.browser.output_invalid",
            retryable=False,
        )
    structured, serialized = _bounded_observation_payload(
        provider,
        observation,
        BrowserNavigateTool.spec.maximum_output_bytes,
    )
    return ToolResult(
        ok=True,
        content=[TextPart(text=serialized)],
        structured=structured,
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )


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
        if (
            not isinstance(url, str)
            or set(arguments) != {"url"}
            or not is_public_https_url(url)
            or not self._provider.allows(url)
        ):
            return _failure(
                ToolFailureKind.INVALID_ARGUMENTS,
                "tool.browser.url_disallowed",
                retryable=False,
            )
        try:
            await bind_browser_execution(self._provider, context)
            observation = await self._provider.navigate(url)
        except BrowserProviderError as error:
            return _provider_failure(error)
        return _observation_result(self._provider, observation)
