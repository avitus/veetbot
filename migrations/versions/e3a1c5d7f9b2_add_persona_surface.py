"""Add persona documents and nominations.

Revision ID: e3a1c5d7f9b2
Revises: c2e0f4a6b831
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e3a1c5d7f9b2"
down_revision: str | Sequence[str] | None = "c2e0f4a6b831"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "persona_documents",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("entries", postgresql.JSONB(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_nomination_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "principal_id", "version", name="pk_persona_documents"
        ),
    )
    op.create_table(
        "persona_nominations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("belief_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("belief_type", sa.Text(), nullable=False),
        sa.Column("authority", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("corroboration_count", sa.Integer(), nullable=False),
        sa.Column("sensitivity", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("consolidation_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("nominated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("affirmed_version", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_persona_nominations"),
    )
    op.create_index(
        "ix_persona_nominations_open",
        "persona_nominations",
        ["tenant_id", "principal_id", "belief_id"],
        unique=True,
        postgresql_where=sa.text("state = 'nominated'"),
    )
    op.create_index(
        "ix_persona_nominations_principal_state",
        "persona_nominations",
        ["tenant_id", "principal_id", "state"],
    )


def downgrade() -> None:
    op.drop_index("ix_persona_nominations_principal_state", table_name="persona_nominations")
    op.drop_index("ix_persona_nominations_open", table_name="persona_nominations")
    op.drop_table("persona_nominations")
    op.drop_table("persona_documents")
