"""Add versioned exposure counters to memories (usage-accounting@2).

Revision ID: d4e7a1c9b3f2
Revises: c0c4e1a7d935

Legacy `utility` was charged for every snapshot member on every run and is
not evidence of non-use. The counters start at zero for every row and are
moved only by the accounting version named on the row; no backfill.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e7a1c9b3f2"
down_revision: str | Sequence[str] | None = "c0c4e1a7d935"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memories",
        sa.Column("delivered_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "memories",
        sa.Column("cited_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("memories", sa.Column("usage_accounting_version", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("memories", "usage_accounting_version")
    op.drop_column("memories", "cited_count")
    op.drop_column("memories", "delivered_count")
