"""Add authoritative session history synchronization and deletion tombstones.

Revision ID: d7e9f1a2b3c4
Revises: c6d8e9f0a1b2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d7e9f1a2b3c4"
down_revision: str | Sequence[str] | None = "c6d8e9f0a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tenant_policy(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_tenant_isolation ON {table} "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def upgrade() -> None:
    op.create_table(
        "session_deletions",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_session_deletions")),
    )
    op.create_index(
        "ix_session_deletions_tenant_principal",
        "session_deletions",
        ["tenant_id", "principal_id", "deleted_at"],
    )
    op.create_table(
        "session_deletion_artifacts",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("artifact", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["session_deletions.session_id"],
            name=op.f("fk_session_deletion_artifacts_session_id_session_deletions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "session_id", "artifact_id", name=op.f("pk_session_deletion_artifacts")
        ),
    )
    _tenant_policy("session_deletions")
    _tenant_policy("session_deletion_artifacts")


def downgrade() -> None:
    op.drop_table("session_deletion_artifacts")
    op.drop_index("ix_session_deletions_tenant_principal", table_name="session_deletions")
    op.drop_table("session_deletions")
