"""Saved job revisions, private grants and a separate publication outbox namespace.

Revision ID: d8a268b10301
Revises: c4e8a2d6f055
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d8a268b10301"
down_revision: str | Sequence[str] | None = "c4e8a2d6f055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add private job storage and isolate job publication from widget publication."""
    op.create_table(
        "agent_jobs",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "principal_id", "slug", name="pk_agent_jobs"),
        sa.CheckConstraint("version >= 1", name="ck_agent_jobs_version"),
    )
    op.execute("ALTER TABLE agent_jobs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE agent_jobs FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY agent_jobs_tenant_isolation ON agent_jobs "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )
    for table in ("cockpit_commit_outbox", "cockpit_import_failures"):
        op.add_column(
            table, sa.Column("package_kind", sa.Text(), nullable=False, server_default="widget")
        )
    op.drop_index("ix_cockpit_commit_outbox_due", table_name="cockpit_commit_outbox")
    op.create_index(
        "ix_cockpit_commit_outbox_due",
        "cockpit_commit_outbox",
        ["tenant_id", "package_kind", "next_attempt_at"],
    )


def downgrade() -> None:
    """Restore the old schema only after job data and publication records are removed."""
    op.execute("SET LOCAL row_security = off")
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM agent_jobs)")):
        raise RuntimeError("Remove saved jobs before downgrade; pinned revisions must not be lost.")
    for table in ("cockpit_commit_outbox", "cockpit_import_failures"):
        if op.get_bind().scalar(
            sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE package_kind = 'job')")
        ):
            raise RuntimeError("Complete job publication before downgrade.")
    op.drop_index("ix_cockpit_commit_outbox_due", table_name="cockpit_commit_outbox")
    for table in ("cockpit_commit_outbox", "cockpit_import_failures"):
        op.drop_column(table, "package_kind")
    op.create_index(
        "ix_cockpit_commit_outbox_due", "cockpit_commit_outbox", ["tenant_id", "next_attempt_at"]
    )
    op.drop_table("agent_jobs")
