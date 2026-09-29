"""Persist owner-managed task approval websites (ADR-0141).

Revision ID: a13c9e724610
Revises: f8db9d463210
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a13c9e724610"
down_revision: str | Sequence[str] | None = "f8db9d463210"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "browser_task_scope_policies",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("scopes", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "principal_id"),
        sa.CheckConstraint("revision >= 0", name="browser_task_scope_revision_nonnegative"),
        sa.CheckConstraint(
            "jsonb_typeof(scopes) = 'array' AND jsonb_array_length(scopes) <= 16",
            name="browser_task_scopes_bounded",
        ),
    )
    op.execute("ALTER TABLE browser_task_scope_policies ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE browser_task_scope_policies FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY browser_task_scope_policies_tenant_isolation "
        "ON browser_task_scope_policies "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def downgrade() -> None:
    op.drop_table("browser_task_scope_policies")
