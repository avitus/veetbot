"""Admit People merge suggestions (ADR-0125).

Revision ID: 524f16dfc8f9
Revises: e5a2c9d7b3f1
"""

from collections.abc import Sequence

from alembic import op

revision: str = "524f16dfc8f9"
down_revision: str | Sequence[str] | None = "e5a2c9d7b3f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = (
    "'person','identifier','source','mention','memory_link','organization',"
    "'relationship','interaction','commitment','operation','erasure','import_job'"
)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_people_heads_people_head_kind"), "people_heads", type_="check")
    op.create_check_constraint(
        op.f("ck_people_heads_people_head_kind"),
        "people_heads",
        f"kind IN ({_KINDS},'merge_suggestion')",
    )


def downgrade() -> None:
    # Suggestions are derived and re-proposed by the next duplicate pass; their
    # revisions and links cascade with the head row.
    op.execute("DELETE FROM people_heads WHERE kind = 'merge_suggestion'")
    op.drop_constraint(op.f("ck_people_heads_people_head_kind"), "people_heads", type_="check")
    op.create_check_constraint(
        op.f("ck_people_heads_people_head_kind"), "people_heads", f"kind IN ({_KINDS})"
    )
