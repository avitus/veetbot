"""Require backfilled source sequence metadata."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ea3210a1b003"
down_revision: str | Sequence[str] | None = "ea3210a1b002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("memories", "memory_revisions"):
        op.alter_column(table, "creation_sequence", existing_type=sa.BigInteger(), nullable=False)


def downgrade() -> None:
    for table in ("memories", "memory_revisions"):
        op.alter_column(table, "creation_sequence", existing_type=sa.BigInteger(), nullable=True)
