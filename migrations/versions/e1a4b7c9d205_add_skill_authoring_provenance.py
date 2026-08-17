"""Add complete Milestone 10A skill-authoring provenance.

Revision ID: e1a4b7c9d205
Revises: d7e9f1a2b3c4
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1a4b7c9d205"
down_revision: str | Sequence[str] | None = "d7e9f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("run_kind", sa.Text(), server_default=sa.text("'interactive'"), nullable=False),
    )
    op.create_index(
        "uq_runs_parent_skill_review",
        "runs",
        ["parent_run_id"],
        unique=True,
        postgresql_where=sa.text("parent_run_id IS NOT NULL AND run_kind = 'skill_review'"),
    )
    op.add_column(
        "skill_revisions",
        sa.Column("authored_by_invocation_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "skill_revisions",
        sa.Column("authoring_idempotency_key", sa.Text(), nullable=True),
    )
    op.add_column(
        "skill_revisions",
        sa.Column("archived_by_invocation_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "skill_revisions",
        sa.Column("archive_idempotency_key", sa.Text(), nullable=True),
    )
    op.execute(
        "UPDATE skill_revisions "
        "SET authored_by_invocation_id = id, "
        "authoring_idempotency_key = 'legacy:' || id::text "
        "WHERE authored_by_run_id IS NOT NULL"
    )
    op.create_index(
        "uq_skill_revisions_authoring_invocation",
        "skill_revisions",
        ["authored_by_invocation_id"],
        unique=True,
        postgresql_where=sa.text("authored_by_invocation_id IS NOT NULL"),
    )
    op.create_index(
        "uq_skill_revisions_archive_invocation",
        "skill_revisions",
        ["archived_by_invocation_id"],
        unique=True,
        postgresql_where=sa.text("archived_by_invocation_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_skill_revisions_archive_invocation", table_name="skill_revisions")
    op.drop_column("skill_revisions", "archive_idempotency_key")
    op.drop_column("skill_revisions", "archived_by_invocation_id")
    op.drop_index("uq_skill_revisions_authoring_invocation", table_name="skill_revisions")
    op.drop_column("skill_revisions", "authoring_idempotency_key")
    op.drop_column("skill_revisions", "authored_by_invocation_id")
    op.drop_index("uq_runs_parent_skill_review", table_name="runs")
    op.drop_column("runs", "run_kind")
