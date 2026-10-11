"""Preview matching without confirming, seeding, reserving or calling a provider."""

from datetime import datetime
from itertools import combinations
from typing import Literal
from uuid import UUID

from agent_core.domain.reconsolidation import ReconValue, compatible, neighbor_score
from agent_core.evals.owner_memory_fixture import FixturePacket, validate_fixture_source


class FixturePreflight(ReconValue):
    pairs: tuple[tuple[UUID, UUID], ...]
    selected_pairs: tuple[tuple[UUID, UUID], ...]
    unmatched_selected: tuple[UUID, ...]
    ready: bool
    reason: Literal[
        "ready",
        "select_two",
        "different_contexts",
        "not_portable",
        "no_topic_overlap",
        "invalid_input",
    ]


def preview_selection(
    packet: FixturePacket, selected: set[UUID], now: datetime
) -> FixturePreflight:
    allowed = set()
    for source in packet.sources:
        try:
            validate_fixture_source(packet, source, now)
        except ValueError:
            continue
        allowed.add(source.reference_id)
    pairs = tuple(
        (left.reference_id, right.reference_id)
        for left, right in combinations(packet.sources, 2)
        if {left.reference_id, right.reference_id} <= allowed
        and (neighbor_score(left, right) is not None or neighbor_score(right, left) is not None)
    )
    selected_pairs = tuple(pair for pair in pairs if set(pair) <= selected)
    matched = {key for pair in selected_pairs for key in pair}
    chosen = [s for s in packet.sources if s.reference_id in selected]
    reason: Literal[
        "ready",
        "select_two",
        "different_contexts",
        "not_portable",
        "no_topic_overlap",
        "invalid_input",
    ]
    if not selected <= allowed:
        reason = "invalid_input"
    elif len(selected) < 2:
        reason = "select_two"
    elif selected_pairs:
        reason = "ready"
    elif any(compatible(a, b) for a, b in combinations(chosen, 2)):
        reason = "no_topic_overlap"
    elif any(a.scope == b.scope for a, b in combinations(chosen, 2)):
        reason = "not_portable"
    else:
        reason = "different_contexts"
    return FixturePreflight(
        pairs=pairs,
        selected_pairs=selected_pairs,
        unmatched_selected=tuple(s.reference_id for s in chosen if s.reference_id not in matched),
        ready=reason == "ready",
        reason=reason,
    )
