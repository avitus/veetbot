"""Resumable semantic inventory and content-free, durable spend reservations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ea3310a1b002"
down_revision: str | Sequence[str] | None = "ea3310a1b001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "semantic_index_jobs",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "principal_id"),
    )
    op.create_table(
        "semantic_index_spend",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("charge", sa.Numeric(20, 8), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("charge >= 0", name="semantic_spend_nonnegative"),
    )
    op.create_index(
        "ix_semantic_spend_owner_day", "semantic_index_spend", ["tenant_id", "principal_id", "day"]
    )
    for table in ("semantic_index_jobs", "semantic_index_spend"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON {table} "
            "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
            "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
        )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute(
        "LOCK TABLE reconsolidation_owners, semantic_index_jobs, semantic_index_spend "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM semantic_index_spend) OR EXISTS (SELECT 1 FROM semantic_index_jobs)
      THEN RAISE EXCEPTION 'semantic indexing work/accounting requires archival'; END IF;
    END $$""")
    op.drop_table("semantic_index_spend")
    op.drop_table("semantic_index_jobs")
