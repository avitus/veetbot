"""Admit source-only memory evidence links (ADR-0233)."""

from collections.abc import Sequence

from alembic import op

revision: str = "e6a3c9d4b217"
down_revision: str | Sequence[str] | None = "d5f2b8c3a216"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = (
    "'person','identifier','source','mention','memory_link','organization',"
    "'relationship','interaction','commitment','operation','erasure','import_job',"
    "'merge_suggestion'"
)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_people_heads_people_head_kind"), "people_heads", type_="check")
    op.create_check_constraint(
        op.f("ck_people_heads_people_head_kind"),
        "people_heads",
        f"kind IN ({_KINDS},'source_memory_link')",
    )


def downgrade() -> None:
    # Attribution includes erased heads. Never silently discard provenance or
    # suppression history; a restricted migration role must fail closed as well.
    op.execute("SET LOCAL row_security = off")
    op.execute("LOCK TABLE people_heads IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM people_heads "
        "WHERE kind = 'source_memory_link') THEN "
        "RAISE EXCEPTION 'Source-learning attribution must be preserved before downgrade'; "
        "END IF; END $$"
    )
    op.drop_constraint(op.f("ck_people_heads_people_head_kind"), "people_heads", type_="check")
    op.create_check_constraint(
        op.f("ck_people_heads_people_head_kind"), "people_heads", f"kind IN ({_KINDS})"
    )
