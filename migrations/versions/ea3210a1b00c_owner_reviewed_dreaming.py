"""Retain daily proposal history and owner decisions without activating dreaming."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ea3210a1b00c"
down_revision: str | Sequence[str] | None = "ea3210a1b00b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "reconsolidation_owners",
        sa.Column(
            "dreaming_state",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.create_table(
        "dreaming_runs",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "principal_id", "id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["reconsolidation_owners.tenant_id", "reconsolidation_owners.principal_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_dreaming_runs_owner_time",
        "dreaming_runs",
        ["tenant_id", "principal_id", "started_at", "id"],
    )
    op.execute("ALTER TABLE dreaming_runs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE dreaming_runs FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY dreaming_runs_tenant_isolation ON dreaming_runs "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute(
        "LOCK TABLE reconsolidation_owners, reconsolidation_jobs, reconsolidation_operations, "
        "dreaming_runs IN ACCESS EXCLUSIVE MODE"
    )
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM dreaming_runs) "
            "OR EXISTS (SELECT 1 FROM reconsolidation_owners WHERE dreaming_state <> '{}'::jsonb) "
            "OR EXISTS (SELECT 1 FROM reconsolidation_operations WHERE payload ? 'owner_review')"
        )
    ):
        raise RuntimeError("Preserve dreaming review and scheduling history before downgrade")
    op.drop_table("dreaming_runs")
    op.drop_column("reconsolidation_owners", "dreaming_state")
