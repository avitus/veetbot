"""Category-independent bounded candidate selection after visibility filtering."""

from collections.abc import Sequence
from uuid import UUID

from agent_core.domain.memory import (
    MemoryRecord,
    RecallQuery,
    is_owned_resource,
    lexical_term_lexemes,
    lexical_tokens,
    recall_query_terms,
)


def candidate_strength(record: MemoryRecord, query: RecallQuery) -> tuple[int, int]:
    """Return structured strength and distinct whole-term coverage, not authority."""

    terms = lexical_term_lexemes(recall_query_terms(query.text))
    tokens = lexical_tokens(f"{record.subject} {record.statement}")
    lexical = sum(bool(term) and term <= tokens for term in terms)
    structured = (
        4
        if record.subject.casefold() in {item.casefold() for item in query.subjects}
        or (query.include_ids is not None and record.id in query.include_ids)
        or record.id in query.expand_ids
        else 2
        if query.include_owned_resources and is_owned_resource(record)
        else 1
        if record.belief_type in query.structured_belief_types
        else 0
    )
    return structured, lexical


def select_candidates(records: Sequence[MemoryRecord], query: RecallQuery) -> list[MemoryRecord]:
    """Merge independently capped arms without letting either starve the other."""

    limit = max(query.max_items * 8, 64)
    if query.candidate_selection == "recent" or not (
        query.text is not None
        or query.subjects
        or query.structured_belief_types
        or query.include_owned_resources
        or query.include_ids is not None
        or query.expand_ids
    ):
        return sorted(
            records,
            key=lambda record: (
                -int(query.include_owned_resources and is_owned_resource(record)),
                -record.store_position,
                str(record.id),
            ),
        )[:limit]
    strengths = {row.id: candidate_strength(row, query) for row in records}
    selected: dict[UUID, MemoryRecord] = {}
    for arm in (0, 1):
        ordered = sorted(
            (row for row in records if strengths[row.id][arm] > 0),
            key=lambda row: (
                -strengths[row.id][arm],
                -strengths[row.id][1 - arm],
                -row.store_position,
                str(row.id),
            ),
        )
        selected.update((row.id, row) for row in ordered[:limit])
    term_count = max(1, len(recall_query_terms(query.text)))
    return sorted(
        selected.values(),
        key=lambda row: (
            -max(strengths[row.id][0] / 4, strengths[row.id][1] / term_count),
            -row.store_position,
            str(row.id),
        ),
    )
