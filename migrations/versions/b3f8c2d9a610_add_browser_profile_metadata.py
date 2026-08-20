"""Add secret-free persistent browser profile metadata.

Revision ID: b3f8c2d9a610
Revises: a4c8e2f6b913
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b3f8c2d9a610"
down_revision: str | Sequence[str] | None = "a4c8e2f6b913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "browser_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("provider_name", sa.Text(), nullable=True),
        sa.Column("provider_ref", sa.Text(), nullable=True),
        sa.Column("allowed_origins", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("encryption_key_version", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "generation >= 0",
            name=op.f("ck_browser_profiles_generation_nonnegative"),
        ),
        sa.CheckConstraint(
            "status IN ('provisioning','authentication_required','ready','needs_user','revoked')",
            name=op.f("ck_browser_profiles_status_closed"),
        ),
        sa.CheckConstraint(
            "(status = 'provisioning' AND provider_name IS NULL "
            "AND provider_ref IS NULL AND encryption_key_version IS NULL) OR "
            "(status = 'revoked' AND ((provider_name IS NULL "
            "AND provider_ref IS NULL AND encryption_key_version IS NULL) OR "
            "(provider_name IS NOT NULL AND provider_ref IS NOT NULL "
            "AND encryption_key_version IS NOT NULL))) OR "
            "(status IN ('authentication_required','ready','needs_user') "
            "AND provider_name IS NOT NULL AND provider_ref IS NOT NULL "
            "AND encryption_key_version IS NOT NULL)",
            name=op.f("ck_browser_profiles_binding_consistent"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_browser_profiles")),
    )
    op.create_index(
        "ix_browser_profiles_tenant_principal_created",
        "browser_profiles",
        ["tenant_id", "principal_id", "created_at"],
    )
    op.create_index(
        "uq_browser_profiles_provider_ref",
        "browser_profiles",
        ["tenant_id", "provider_ref"],
        unique=True,
        postgresql_where=sa.text("provider_ref IS NOT NULL"),
    )
    op.execute("ALTER TABLE browser_profiles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE browser_profiles FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY browser_profiles_tenant_isolation ON browser_profiles "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def downgrade() -> None:
    op.drop_index("uq_browser_profiles_provider_ref", table_name="browser_profiles")
    op.drop_index(
        "ix_browser_profiles_tenant_principal_created",
        table_name="browser_profiles",
    )
    op.drop_table("browser_profiles")
