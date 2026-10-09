"""Durable owner-scoped receipts for derived-memory writes."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ea3210a1b008"
down_revision: str | Sequence[str] | None = "ea3210a1b007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reconsolidation_memory_writes",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("key_digest", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.UUID(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "principal_id", "key_digest"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id", "operation_id"],
            [
                "reconsolidation_operations.tenant_id",
                "reconsolidation_operations.principal_id",
                "reconsolidation_operations.id",
            ],
            ondelete="CASCADE",
        ),
    )
    op.execute("ALTER TABLE reconsolidation_memory_writes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE reconsolidation_memory_writes FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY reconsolidation_memory_writes_tenant_isolation "
        "ON reconsolidation_memory_writes "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute("LOCK TABLE reconsolidation_owners, reconsolidation_jobs IN ACCESS EXCLUSIVE MODE")
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM reconsolidation_jobs WHERE state = 'running') "
                "OR EXISTS (SELECT 1 FROM reconsolidation_memory_writes) "
                "OR EXISTS (SELECT 1 FROM reconsolidation_blocks "
                "WHERE reason = 'owner_summary_rejection') "
                "OR EXISTS (SELECT 1 FROM reconsolidation_operations "
                "WHERE payload->>'kind' = 'summary' "
                "AND payload ? 'owner_revision') "
                "OR EXISTS (SELECT 1 FROM reconsolidation_summaries "
                "WHERE payload ? 'flagged_for_review')"
            )
        )
        .scalar()
    ):
        raise RuntimeError(
            "Disable reconsolidation, finish leased work and export its history and owner controls "
            "before downgrade; "
            "derived memory receipts and suppression must not be lost"
        )
    op.drop_table("reconsolidation_memory_writes")
