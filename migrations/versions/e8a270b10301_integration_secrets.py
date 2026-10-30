"""Owner-bound encrypted integration keys and revocation tombstones.

Revision ID: e8a270b10301
Revises: d8a268b10301
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e8a270b10301"
down_revision: str | Sequence[str] | None = "d8a268b10301"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "integration_secrets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("auth_scheme", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("sealed_value", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("revision > 0", name="integration_secrets_revision"),
        sa.CheckConstraint(
            "(deleted_at IS NULL) = (sealed_value IS NOT NULL)",
            name="integration_secrets_lifecycle",
        ),
    )
    op.create_index("ix_integration_secrets_tenant_id", "integration_secrets", ["tenant_id"])
    op.execute("ALTER TABLE integration_secrets ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE integration_secrets FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY integration_secrets_tenant_isolation ON integration_secrets "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM integration_secrets)")):
        raise RuntimeError("Integration keys and revocation tombstones must not be lost.")
    op.drop_table("integration_secrets")
