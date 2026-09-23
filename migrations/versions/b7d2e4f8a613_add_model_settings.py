"""Add the owner's versioned model settings (ADR-0119).

Revision ID: b7d2e4f8a613
Revises: e6b8d2a4c901
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d2e4f8a613"
down_revision: str | Sequence[str] | None = "e6b8d2a4c901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_settings",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("chat_model_policy", sa.Text(), nullable=False),
        sa.Column("chat_reasoning_effort", sa.Text(), nullable=True),
        sa.Column("memory_model_policy", sa.Text(), nullable=False),
        sa.Column("memory_reasoning_effort", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "principal_id", "version"),
        sa.CheckConstraint("version > 0", name="model_settings_version_positive"),
    )
    op.execute("ALTER TABLE model_settings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE model_settings FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY model_settings_tenant_isolation ON model_settings "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def downgrade() -> None:
    op.drop_table("model_settings")
