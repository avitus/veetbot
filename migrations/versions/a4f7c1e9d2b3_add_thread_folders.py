"""Add chat thread folders, session memberships, and grouping proposals.

Revision ID: a4f7c1e9d2b3
Revises: c28d52ea7301
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a4f7c1e9d2b3"
down_revision: str | Sequence[str] | None = "c28d52ea7301"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "thread_folders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("name_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_thread_folders"),
        sa.UniqueConstraint(
            "tenant_id",
            "principal_id",
            "name_key",
            name="uq_thread_folders_principal_name_key",
        ),
    )
    op.create_table(
        "session_folder_memberships",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("folder_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_session_folder_memberships_session_id_sessions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["folder_id"],
            ["thread_folders.id"],
            name="fk_session_folder_memberships_folder_id_thread_folders",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("session_id", name="pk_session_folder_memberships"),
    )
    op.create_index(
        "ix_session_folder_memberships_folder_id",
        "session_folder_memberships",
        ["folder_id"],
    )
    op.create_table(
        "thread_folder_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("proposed_name", sa.Text(), nullable=True),
        sa.Column("target_folder_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("member_session_ids", postgresql.JSONB(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("derivation", sa.Text(), nullable=False),
        sa.Column("content_key", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("withdrawal_reason", sa.Text(), nullable=True),
        sa.Column("resulting_folder_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_thread_folder_proposals"),
    )
    op.create_index(
        "ix_thread_folder_proposals_open",
        "thread_folder_proposals",
        ["tenant_id", "principal_id", "content_key"],
        unique=True,
        postgresql_where=sa.text("state = 'proposed'"),
    )
    op.create_index(
        "ix_thread_folder_proposals_principal_state",
        "thread_folder_proposals",
        ["tenant_id", "principal_id", "state"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_thread_folder_proposals_principal_state", table_name="thread_folder_proposals"
    )
    op.drop_index("ix_thread_folder_proposals_open", table_name="thread_folder_proposals")
    op.drop_table("thread_folder_proposals")
    op.drop_index(
        "ix_session_folder_memberships_folder_id", table_name="session_folder_memberships"
    )
    op.drop_table("session_folder_memberships")
    op.drop_table("thread_folders")
