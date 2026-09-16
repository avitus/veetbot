"""Indexed, content-free reference predicates for generated People copies."""

from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.sql.elements import TextClause

INVOCATION_REFERENCE_TEXT = (
    "coalesce(raw_arguments, '') || coalesce(arguments::text, '') || "
    "coalesce(result_item::text, '') || coalesce(structured_result::text, '') || "
    "coalesce(outcome::text, '') || coalesce(policy_decision::text, '')"
)


def reference_overlap(expression: str, identifiers: list[UUID]) -> TextClause:
    """Bind one UUID array, including beyond the PostgreSQL parameter limit."""
    return text(f"people_reference_ids({expression}) && :people_reference_ids").bindparams(
        bindparam("people_reference_ids", identifiers, type_=ARRAY(PGUUID(as_uuid=True)))
    )
