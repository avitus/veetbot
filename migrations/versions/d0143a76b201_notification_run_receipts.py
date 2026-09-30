"""Add principal-scoped terminal-result notification receipts (ADR-0143)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d0143a76b201"
down_revision: str | Sequence[str] | None = "c978eb0bac0f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_run_receipts",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "principal_id", "run_id"),
    )
    op.create_index("ix_notification_run_receipts_run", "notification_run_receipts", ["run_id"])
    op.execute("ALTER TABLE notification_run_receipts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE notification_run_receipts FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY notification_run_receipts_tenant_isolation ON notification_run_receipts "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def downgrade() -> None:
    op.drop_table("notification_run_receipts")
