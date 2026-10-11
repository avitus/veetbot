"""Opaque reverse-attribution keys; no names, statements or evidence excerpts."""

from uuid import UUID

from pydantic import Field

from agent_core.domain.people import PeopleRecord, PeopleSource, PersonMemoryLink, PersonMention
from agent_core.domain.reconsolidation import ReconValue


class AttributionFootprint(ReconValue):
    belief_id: UUID | None = None
    source_id: UUID | None = None
    session_id: UUID | None = None
    event_sequence: int | None = Field(default=None, gt=0)


def attribution_footprint(record: PeopleRecord) -> AttributionFootprint:
    if isinstance(record, PersonMemoryLink):
        return AttributionFootprint(belief_id=record.belief_id)
    if isinstance(record, PersonMention):
        return AttributionFootprint(source_id=record.source_id)
    if isinstance(record, PeopleSource):
        return AttributionFootprint(
            source_id=record.id, session_id=record.session_id, event_sequence=record.event_sequence
        )
    return AttributionFootprint()


def attribution_targets(
    footprints: list[AttributionFootprint],
) -> tuple[set[UUID], set[tuple[UUID, int]]]:
    return (
        {item.belief_id for item in footprints if item.belief_id is not None},
        {
            (item.session_id, item.event_sequence)
            for item in footprints
            if item.session_id is not None and item.event_sequence is not None
        },
    )
