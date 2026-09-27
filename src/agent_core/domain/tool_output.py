"""Deterministic excerpts of tool data, before any trust framing is applied."""

from __future__ import annotations

import json

from agent_core.domain.messages import (
    ContentPart,
    ConversationItem,
    FileReferencePart,
    TextPart,
    ToolResultItem,
)


def content_bytes(content: list[ContentPart]) -> bytes:
    return json.dumps(
        [part.model_dump(mode="json") for part in content],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def output_excerpt(
    rendered: bytes,
    *,
    budget: int,
    location: str,
    capture_label: str = "full output",
    reference: FileReferencePart | None = None,
) -> list[ContentPart]:
    """Keep 60% head / 20% tail, shrinking to fit JSON, marker and reference.

    Callers retain the source bytes at ``location``. This is a mechanical
    excerpt, never a summary or a change to the source's trust label.
    """

    def candidate(allowance: int) -> list[ContentPart]:
        head_bytes = allowance * 3 // 4
        tail_bytes = allowance - head_bytes
        head = rendered[:head_bytes].decode("utf-8", errors="ignore")
        tail = rendered[-tail_bytes:].decode("utf-8", errors="ignore") if tail_bytes else ""
        elided = len(rendered) - len(head.encode()) - len(tail.encode())
        marker = f"\n[... {elided:,} bytes elided; {capture_label}: {location} ...]\n"
        return [TextPart(text=head + marker + tail), *([] if reference is None else [reference])]

    if len(content_bytes(candidate(0))) > budget:
        raise ValueError("tool output budget cannot hold its capture reference")
    low, high = 0, min(len(rendered), budget * 4 // 5)
    while low < high:
        middle = (low + high + 1) // 2
        if len(content_bytes(candidate(middle))) <= budget:
            low = middle
        else:
            high = middle - 1
    return candidate(low)


def context_view(item: ConversationItem) -> ConversationItem:
    """Select model presentation without rewriting canonical tool evidence."""
    if isinstance(item, ToolResultItem) and item.context_content is not None:
        return item.model_copy(
            update={"content": item.context_content, "context_content": None},
            deep=True,
        )
    return item.model_copy(deep=True)
