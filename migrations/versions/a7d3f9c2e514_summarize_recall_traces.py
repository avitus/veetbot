"""Give each recall trace a small summary the health report can aggregate.

A trace document is tens of kilobytes and stored out of line, so the recall
health report's aggregates read every document in the window and passed their
ten-second statement bound once automated sessions wrote thousands of traces
a week. The summary holds only identifier-free figures: total recall time,
the arms that ran, and returned and dropped counts by review class. It is
null on traces recorded before this revision, and the report counts those
apart rather than reading their documents. The index serves the report's
owner and time window.

Revision ID: a7d3f9c2e514
Revises: f3a1c0de0324
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7d3f9c2e514"
down_revision: str | Sequence[str] | None = "f3a1c0de0324"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "recall_traces",
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index(
        "ix_recall_traces_owner_created",
        "recall_traces",
        ["tenant_id", "principal_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_recall_traces_owner_created", table_name="recall_traces")
    op.drop_column("recall_traces", "summary")
