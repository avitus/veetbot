"""Deterministic contiguous-suffix history selection and pair validation."""

from __future__ import annotations

from collections.abc import Sequence

from agent_core.domain.messages import ConversationItem, ToolCallItem, ToolResultItem
from agent_core.ports.context import TokenEstimator


def _sequence(item: ConversationItem) -> int | None:
    return getattr(item, "source_event_sequence", None)


def _pair_safe_cut(items: Sequence[ConversationItem], cut: int) -> int:
    """Move a cut later until the retained suffix contains no orphaned pair."""

    while cut < len(items):
        retained = items[cut:]
        calls = {item.call_id for item in retained if isinstance(item, ToolCallItem)}
        results = {item.call_id for item in retained if isinstance(item, ToolResultItem)}
        orphan_ids = calls ^ results
        if not orphan_ids:
            return cut
        orphan_positions = [
            index
            for index, item in enumerate(retained, start=cut)
            if isinstance(item, (ToolCallItem, ToolResultItem)) and item.call_id in orphan_ids
        ]
        if not orphan_positions:
            return cut
        cut = max(orphan_positions) + 1
    return cut


def select_history(
    items: Sequence[ConversationItem],
    summary_floor: int,
    history_tokens: int,
    estimator: TokenEstimator,
    model_id: str,
) -> int:
    """Return the index of the largest pair-safe suffix within the token budget."""

    if summary_floor < 0 or history_tokens < 0:
        raise ValueError("history floor and token budget must be nonnegative")
    floor_index = 0
    for index, item in enumerate(items):
        sequence = _sequence(item)
        if sequence is not None and sequence <= summary_floor:
            floor_index = index + 1

    # TokenEstimator estimates are required to be monotonic for contiguous
    # suffixes: removing a leading item cannot increase the estimate. Binary
    # search therefore finds the earliest fitting suffix without quadratic
    # reserialization of every candidate.
    low = floor_index
    high = len(items)
    while low < high:
        candidate = (low + high) // 2
        if estimator.estimate(items[candidate:], model_id) <= history_tokens:
            high = candidate
        else:
            low = candidate + 1
    cut = low
    cut = _pair_safe_cut(items, cut)
    return max(cut, floor_index)


def validate_tool_pairs(items: Sequence[ConversationItem]) -> None:
    calls = [item.call_id for item in items if isinstance(item, ToolCallItem)]
    results = [item.call_id for item in items if isinstance(item, ToolResultItem)]
    if len(calls) != len(set(calls)) or len(results) != len(set(results)):
        raise ValueError("assembled context contains duplicate tool pair identifiers")
    if set(calls) != set(results):
        raise ValueError("assembled context contains an orphaned tool call or result")
