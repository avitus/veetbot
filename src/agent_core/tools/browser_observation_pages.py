"""Complete browser-result pages that fit before generic output admission."""

from __future__ import annotations

import json
from typing import Any

from agent_core.domain.browser import BrowserObservation
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import TrustLevel
from agent_core.domain.tool_output import content_bytes
from agent_core.domain.tools import ToolFailureKind, ToolResult
from agent_core.ports.browser import BrowserProvider
from agent_core.tools.browser_results import (
    ELEMENT_SCHEMA,
    OUTPUT_SCHEMA,
    browser_failure,
    observation_evidence_key,
)

_PAGE_PROPERTIES: dict[str, Any] = {
    "element_offset": {"type": "integer", "minimum": 0},
    "next_element_offset": {"type": ["integer", "null"]},
    "total_elements": {"type": "integer", "minimum": 0},
    "text_offset": {"type": "integer", "minimum": 0},
    "next_text_offset": {"type": ["integer", "null"]},
    "total_text_characters": {"type": "integer", "minimum": 0},
    "title_truncated": {"type": "boolean"},
}
PAGE_SCHEMA: dict[str, Any] = {
    **OUTPUT_SCHEMA,
    "properties": {
        **OUTPUT_SCHEMA["properties"],
        **_PAGE_PROPERTIES,
        "elements": {
            "type": "array",
            "maxItems": 256,
            "items": {
                **ELEMENT_SCHEMA,
                "properties": {
                    **ELEMENT_SCHEMA["properties"],
                    "name_truncated": {"type": "boolean"},
                },
                "required": [*ELEMENT_SCHEMA["required"], "name_truncated"],
            },
        },
    },
    "required": [*OUTPUT_SCHEMA["required"], *_PAGE_PROPERTIES],
}


def _display(value: str, limit: int) -> tuple[str, bool]:
    """Bound a display label in UTF-8 bytes and indicate whether it was shortened."""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    return encoded[: limit - 3].decode("utf-8", errors="ignore") + "…", True


def _content(page: dict[str, Any]) -> list[TextPart]:
    """Serialize a complete page using the same text content admitted to model context."""
    return [TextPart(text=json.dumps(page, ensure_ascii=False, separators=(",", ":")))]


def _size(page: dict[str, Any]) -> int:
    """Measure serialized content bytes, including the text-part envelope and escaping."""
    return len(content_bytes(list(_content(page))))


def observation_page_result(
    provider: BrowserProvider,
    observation: BrowserObservation,
    maximum_bytes: int,
    *,
    element_offset: int,
    text_offset: int,
) -> ToolResult:
    """Build a fresh observation slice that preserves references within the inline budget."""

    def invalid() -> ToolResult:
        """Reject an observation page whose identity or next complete item cannot fit."""
        return browser_failure(
            ToolFailureKind.OUTPUT_INVALID, "tool.browser.output_invalid", retryable=False
        )

    if not provider.allows(observation.url):
        return invalid()
    elements = observation.elements[:256]
    start = min(element_offset, len(elements))
    text_start = min(text_offset, len(observation.text))
    title, title_truncated = _display(observation.title or "", 128)
    page: dict[str, Any] = {
        "provider": provider.name,
        "url": observation.url,
        "title": title if observation.title is not None else None,
        "title_truncated": title_truncated,
        "revision": observation.revision,
        "element_offset": start,
        "next_element_offset": start if start < len(elements) else None,
        "total_elements": len(elements),
        "text_offset": text_start,
        "next_text_offset": text_start if text_start < len(observation.text) else None,
        "total_text_characters": len(observation.text),
        "elements": [],
        "text": "",
    }
    if _size(page) > maximum_bytes:
        return invalid()
    # Reserve some room for page text, but never strand a control just because
    # it needs that allowance. Complete references and states are never cut.
    reserve = min(512, maximum_bytes // 4) if text_start < len(observation.text) else 0
    for index in range(start, len(elements)):
        item = elements[index].model_dump(mode="json")
        item["name"], item["name_truncated"] = _display(item["name"], 256)
        previous_next = page["next_element_offset"]
        page["elements"].append(item)
        page["next_element_offset"] = index + 1 if index + 1 < len(elements) else None
        ceiling = maximum_bytes - reserve if len(page["elements"]) > 1 else maximum_bytes
        if _size(page) > ceiling:
            page["elements"].pop()
            page["next_element_offset"] = previous_next
            break
    if start < len(elements) and not page["elements"]:
        return invalid()
    # Offsets count Python/Unicode characters; escaped quotes and multi-byte
    # characters count their actual serialized bytes against the admission cap.
    low, high = 0, len(observation.text) - text_start
    while low < high:
        count = (low + high + 1) // 2
        end = text_start + count
        page["text"] = observation.text[text_start:end]
        page["next_text_offset"] = end if end < len(observation.text) else None
        if _size(page) <= maximum_bytes:
            low = count
        else:
            high = count - 1
    end = text_start + low
    page["text"] = observation.text[text_start:end]
    page["next_text_offset"] = end if end < len(observation.text) else None
    if not page["elements"] and low == 0 and end < len(observation.text):
        return invalid()
    return ToolResult(
        ok=True,
        content=list(_content(page)),
        structured=page,
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        evidence_key=observation_evidence_key(page),
    )
