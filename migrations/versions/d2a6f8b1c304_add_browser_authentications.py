"""Add secret-free browser authentication records.

Revision ID: d2a6f8b1c304
Revises: c7d1e4a9f205
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2a6f8b1c304"
down_revision: str | Sequence[str] | None = "c7d1e4a9f205"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "browser_authentications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('authentication_required','needs_user','ready','expired','cancelled')",
            name=op.f("ck_browser_authentications_status_closed"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + interval '5 minutes'",
            name=op.f("ck_browser_authentications_time_window"),
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["browser_profiles.id"],
            name=op.f("fk_browser_authentications_profile_id_browser_profiles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_browser_authentications")),
    )
    op.create_index(
        "ix_browser_authentications_tenant_principal_profile",
        "browser_authentications",
        ["tenant_id", "principal_id", "profile_id", "created_at"],
    )
    op.execute("ALTER TABLE browser_authentications ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE browser_authentications FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY browser_authentications_tenant_isolation "
        "ON browser_authentications "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_browser_authentications_tenant_principal_profile",
        table_name="browser_authentications",
    )
    op.drop_table("browser_authentications")
