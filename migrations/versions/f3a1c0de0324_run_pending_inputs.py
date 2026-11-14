"""Hold follow-ups for a working run until it reads them (ADR-0324).

A row lives from the moment the API accepts a follow-up until its run reads or
drops it, so a follow-up's content is never stored here and in the event log at
once. `position` records arrival order. The foreign key is to the session, not
the run, so an insert never waits on the run row its worker holds locked.

Revision ID: f3a1c0de0324
Revises: e6b2d9f4a1c7
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f3a1c0de0324"
down_revision: str | Sequence[str] | None = "e6b2d9f4a1c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_pending_inputs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_run_pending_inputs_session_id_sessions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_run_pending_inputs"),
    )
    op.create_index(
        "ix_run_pending_inputs_run_id_position", "run_pending_inputs", ["run_id", "position"]
    )


def downgrade() -> None:
    op.drop_index("ix_run_pending_inputs_run_id_position", table_name="run_pending_inputs")
    op.drop_table("run_pending_inputs")
