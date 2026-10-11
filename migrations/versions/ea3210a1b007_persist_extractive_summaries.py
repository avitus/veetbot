"""Separate erasable extractive content from opaque reconsolidation history."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ea3210a1b007"
down_revision: str | Sequence[str] | None = "ea3210a1b006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reconsolidation_summaries",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.UUID(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "principal_id", "operation_id"),
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
    op.execute("ALTER TABLE reconsolidation_summaries ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE reconsolidation_summaries FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY reconsolidation_summaries_tenant_isolation ON reconsolidation_summaries "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute("LOCK TABLE reconsolidation_owners, reconsolidation_jobs IN ACCESS EXCLUSIVE MODE")
    if (
        op.get_bind()
        .execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM reconsolidation_jobs WHERE state = 'running')")
        )
        .scalar()
    ):
        raise RuntimeError(
            "Disable reconsolidation, finish leased work and export its history before downgrade; "
            "derived summary history and content would be lost"
        )
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM reconsolidation_operations "
                "WHERE payload->>'kind' = 'summary')"
            )
        )
        .scalar()
    ):
        raise RuntimeError(
            "Export and explicitly remove derived summary history before downgrade; "
            "summary history and content would be lost"
        )
    op.drop_table("reconsolidation_summaries")
