"""Add the predicate key to memories (ADR-0248).

Revision ID: a9c3e7f1b2d4
Revises: d4e7a1c9b3f2

Which attribute of its subject a statement claims, derived from the statement
at formation. Nullable, no backfill: legacy rows are keyed at read time.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9c3e7f1b2d4"
down_revision: str | Sequence[str] | None = "d4e7a1c9b3f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("memories", sa.Column("predicate_key", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("memories", "predicate_key")
