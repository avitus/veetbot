"""Rank each permitted SQL recall arm before its limit (ADR-0178)."""

from typing import Any

from sqlalchemy import Select, case, func, literal, or_, union_all
from sqlalchemy.sql.elements import ColumnElement

from agent_core.domain.memory import RecallQuery, recall_query_terms


def ranked_candidates(
    base: Select[Any],
    query: RecallQuery,
    *,
    identifier: Any,
    subject: Any,
    statement: Any,
    belief_type: Any,
    position: Any,
    owned_resource: ColumnElement[bool],
) -> Select[Any]:
    """The base already contains all hard filters, including historical fences."""

    limit = max(query.max_items * 8, 64)
    if query.candidate_selection == "recent" or not (
        query.text is not None
        or query.subjects
        or query.structured_belief_types
        or query.include_owned_resources
        or query.include_ids is not None
        or query.expand_ids
    ):
        return base.order_by(
            *([owned_resource.desc()] if query.include_owned_resources else []),
            position.desc(),
            identifier,
        ).limit(limit)
    terms = recall_query_terms(query.text)
    vector = func.to_tsvector("simple", subject + " " + statement)
    lexical = sum(
        (
            case((vector.op("@@")(func.plainto_tsquery("simple", term)), 1), else_=0)
            for term in terms
        ),
        literal(0),
    )
    exact = or_(
        func.lower(subject).in_(tuple(item.casefold() for item in query.subjects)),
        identifier.in_(query.include_ids or ()),
        identifier.in_(query.expand_ids),
    )
    structured = case(
        (exact, 4),
        (owned_resource, 2),
        (belief_type.in_(tuple(item.value for item in query.structured_belief_types)), 1),
        else_=0,
    )
    keys = base.with_only_columns(identifier, maintain_column_froms=True)
    arms = union_all(
        keys.where(structured > 0)
        .order_by(structured.desc(), lexical.desc(), position.desc(), identifier)
        .limit(limit),
        keys.where(lexical > 0)
        .order_by(lexical.desc(), structured.desc(), position.desc(), identifier)
        .limit(limit),
    ).subquery()
    relevance = func.greatest(structured / 4.0, lexical / float(max(1, len(terms))))
    return base.where(identifier.in_(Select(arms))).order_by(
        relevance.desc(), position.desc(), identifier
    )
