"""Add principal-scoped email experience records.

Revision ID: a8e6c3f9d127
Revises: d8f3a1c7b205
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a8e6c3f9d127"
down_revision: str | Sequence[str] | None = "d8f3a1c7b205"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_records",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(collation="C"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "principal_id", "kind", "key"),
        sa.CheckConstraint("revision > 0", name="email_record_revision_positive"),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="email_record_payload_object"),
    )
    op.create_index(
        "ix_email_records_owner_updated",
        "email_records",
        ["tenant_id", "principal_id", "updated_at"],
    )
    op.execute("ALTER TABLE email_records ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE email_records FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY email_records_tenant_isolation ON email_records "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def downgrade() -> None:
    op.drop_index("ix_email_records_owner_updated", table_name="email_records")
    op.drop_table("email_records")
