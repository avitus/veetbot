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
    # The owner's answer is final (ADR-0125), so a downgrade never erases one. Open
    # and withdrawn suggestions are derived and re-proposed by the next duplicate
    # pass; their revisions and links cascade with the head row.
    op.execute("SET LOCAL row_security = off")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM people_heads AS head "
        "JOIN people_revisions AS revision ON revision.tenant_id = head.tenant_id "
        "AND revision.principal_id = head.principal_id "
        "AND revision.entity_id = head.id AND revision.revision = head.revision "
        "WHERE head.kind = 'merge_suggestion' "
        "AND revision.payload->>'state' IN ('separated', 'merged')) THEN "
        "RAISE EXCEPTION 'People merge suggestions the owner answered must be "
        "exported and erased before downgrade'; "
        "END IF; END $$"
    )
    op.execute("DELETE FROM people_heads WHERE kind = 'merge_suggestion'")
    op.drop_constraint(op.f("ck_people_heads_people_head_kind"), "people_heads", type_="check")
    op.create_check_constraint(
        op.f("ck_people_heads_people_head_kind"), "people_heads", f"kind IN ({_KINDS})"
    )
