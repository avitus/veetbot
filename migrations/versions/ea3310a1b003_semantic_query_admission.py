"""Bounded owner query admission on the existing durable embedding ledger."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ea3310a1b003"
down_revision: str | Sequence[str] | None = "ea3310a1b002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_semantic_pending_query",
        "semantic_index_spend",
        ["tenant_id", "principal_id"],
        unique=True,
        postgresql_where=sa.text("payload->>'kind' = 'query' AND payload->>'state' = 'reserved'"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute("LOCK TABLE reconsolidation_owners, semantic_index_spend IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM semantic_index_spend WHERE payload->>'kind' = 'query'
                 AND payload->>'state' = 'reserved')
      THEN RAISE EXCEPTION 'pending semantic query requires settlement'; END IF;
    END $$""")
    op.drop_index("uq_semantic_pending_query", table_name="semantic_index_spend")
