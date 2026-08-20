"""Shared bounded result and failure conversion for browser tools."""

from __future__ import annotations

import json
from typing import Any

from agent_core.domain.browser import BrowserObservation, BrowserProviderError
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import TrustLevel
from agent_core.domain.tools import ToolFailure, ToolFailureKind, ToolResult
from agent_core.ports.browser import BrowserProvider

MAX_TEXT_BYTES = 256 * 1024
MAX_PROVIDER_BYTES = 128
MAX_URL_BYTES = 4 * 1024
MAX_TITLE_BYTES = 4 * 1024
MAX_REVISION_BYTES = 512
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

_FAILURE_KINDS = {
    "tool.browser.profile_unavailable": ToolFailureKind.PERMISSION,
    "tool.browser.authentication_required": ToolFailureKind.PERMISSION,
    "tool.browser.needs_user": ToolFailureKind.PERMISSION,
    "tool.browser.output_invalid": ToolFailureKind.OUTPUT_INVALID,
    "tool.browser.provider_unavailable": ToolFailureKind.TRANSPORT,
    "tool.browser.outcome_unknown": ToolFailureKind.OUTCOME_UNKNOWN,
    "tool.browser.element_not_found": ToolFailureKind.NOT_FOUND,
    "tool.browser.page_changed": ToolFailureKind.INVALID_ARGUMENTS,
    "tool.browser.action_not_allowed": ToolFailureKind.INVALID_ARGUMENTS,
}


def browser_failure(
    error_or_kind: BrowserProviderError | ToolFailureKind,
    reason_code: str | None = None,
    *,
    retryable: bool | None = None,
) -> ToolResult:
    if isinstance(error_or_kind, BrowserProviderError):
        reason = error_or_kind.reason_code
        kind = _FAILURE_KINDS.get(reason, ToolFailureKind.UPSTREAM_ERROR)
        can_retry = error_or_kind.retryable
    else:
        if reason_code is None or retryable is None:
            raise ValueError("explicit browser failures require reason and retryability")
        kind = error_or_kind
        reason = reason_code
        can_retry = retryable
    return ToolResult(
        ok=False,
        content=[],
        failure=ToolFailure(
            kind=kind,
            reason_code=reason,
            detail="browser access failed at a platform-controlled boundary",
            retryable=can_retry,
        ),
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )


def _bounded_utf8(value: str, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    return encoded[:maximum_bytes].decode("utf-8", errors="ignore")


def _serialized_observation(structured: dict[str, Any]) -> str:
    return json.dumps(structured, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def bounded_observation_payload(
    provider: BrowserProvider,
    observation: BrowserObservation,
    maximum_bytes: int,
) -> tuple[dict[str, Any], str]:
    elements = [element.model_dump(mode="json") for element in observation.elements[:256]]
    base: dict[str, Any] = {
        "provider": _bounded_utf8(provider.name, MAX_PROVIDER_BYTES),
        "url": _bounded_utf8(observation.url, MAX_URL_BYTES),
        "title": (
            _bounded_utf8(observation.title, MAX_TITLE_BYTES)
            if observation.title is not None
            else None
        ),
        "revision": _bounded_utf8(observation.revision, MAX_REVISION_BYTES),
        "text": "",
        "elements": elements,
    }
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
    while low <= high:
        candidate_bytes = (low + high) // 2
        base["text"] = _bounded_utf8(observation.text, candidate_bytes)
        if len(_serialized_observation(base).encode("utf-8")) <= maximum_bytes:
            low = candidate_bytes + 1
        else:
            high = candidate_bytes - 1
    base["text"] = _bounded_utf8(observation.text, max(0, high))
    return base, _serialized_observation(base)


def observation_result(
    provider: BrowserProvider,
    observation: BrowserObservation,
    maximum_bytes: int,
) -> ToolResult:
    if not provider.allows(observation.url):
        return browser_failure(
            ToolFailureKind.OUTPUT_INVALID,
            "tool.browser.output_invalid",
            retryable=False,
        )
    structured, serialized = bounded_observation_payload(provider, observation, maximum_bytes)
    return ToolResult(
        ok=True,
        content=[TextPart(text=serialized)],
        structured=structured,
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )
