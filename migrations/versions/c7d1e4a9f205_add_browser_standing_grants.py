"""Add bounded, secret-free standing browser grants.

Revision ID: c7d1e4a9f205
Revises: b3f8c2d9a610
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7d1e4a9f205"
down_revision: str | Sequence[str] | None = "b3f8c2d9a610"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "browser_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("profile_generation", sa.Integer(), nullable=False),
        sa.Column("agent_version", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("allowed_origins", postgresql.JSONB(), nullable=False),
        sa.Column("action_kinds", postgresql.JSONB(), nullable=False),
        sa.Column("element_roles", postgresql.JSONB(), nullable=False),
        sa.Column("element_names", postgresql.JSONB(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "profile_generation >= 0",
            name=op.f("ck_browser_grants_generation_nonnegative"),
        ),
        sa.CheckConstraint(
            "expires_at > starts_at AND expires_at <= starts_at + interval '30 days'",
            name=op.f("ck_browser_grants_time_window"),
        ),
        sa.CheckConstraint(
            "jsonb_array_length(action_kinds) > 0",
            name=op.f("ck_browser_grants_action_kinds_nonempty"),
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["browser_profiles.id"],
            name=op.f("fk_browser_grants_profile_id_browser_profiles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_browser_grants")),
    )
    op.create_index(
        "ix_browser_grants_tenant_principal_profile",
        "browser_grants",
        ["tenant_id", "principal_id", "profile_id"],
    )
    op.create_index(
        "ix_browser_grants_active_expiry",
        "browser_grants",
        ["expires_at", "revoked_at"],
    )
    op.execute("ALTER TABLE browser_grants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE browser_grants FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY browser_grants_tenant_isolation ON browser_grants "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def downgrade() -> None:
    op.drop_index("ix_browser_grants_active_expiry", table_name="browser_grants")
    op.drop_index(
        "ix_browser_grants_tenant_principal_profile",
        table_name="browser_grants",
    )
    op.drop_table("browser_grants")
