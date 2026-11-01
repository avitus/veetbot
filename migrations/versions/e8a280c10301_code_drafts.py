"""Owner-scoped code drafts that also serve as the proposal outbox (ADR-0281).

Revision ID: e8a280c10301
Revises: e8a270b10301
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e8a280c10301"
down_revision: str | Sequence[str] | None = "e8a270b10301"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the draft table with forced tenant isolation."""
    op.create_table(
        "code_drafts",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("base_release", sa.Text(), nullable=False),
        sa.Column("base_revision", sa.Text(), nullable=False),
        sa.Column("files", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("proposal", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "principal_id", "slug", name="pk_code_drafts"),
        sa.CheckConstraint("version >= 1", name="ck_code_drafts_version"),
        sa.CheckConstraint(
            "state IN ('open', 'proposing', 'proposed', 'discarded')", name="ck_code_drafts_state"
        ),
        sa.CheckConstraint(
            "(proposal IS NULL) = (next_attempt_at IS NULL)", name="ck_code_drafts_next_attempt_at"
        ),
    )
    op.create_index(
        "ix_code_drafts_due_proposals",
        "code_drafts",
        ["tenant_id", "next_attempt_at"],
        postgresql_where=sa.text("state = 'proposing'"),
    )
    op.execute("ALTER TABLE code_drafts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE code_drafts FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY code_drafts_tenant_isolation ON code_drafts "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def downgrade() -> None:
    """Drop the table only once no draft or pending proposal would be lost."""
    op.execute("SET LOCAL row_security = off")
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM code_drafts)")):
        raise RuntimeError(
            "Remove code drafts before downgrade; pending proposals must not be lost."
        )
    op.drop_table("code_drafts")
