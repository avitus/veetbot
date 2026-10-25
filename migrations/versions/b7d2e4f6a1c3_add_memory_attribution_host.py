"""Add the attribution host to memories (ADR-0250).

Revision ID: b7d2e4f6a1c3
Revises: a9c3e7f1b2d4

The website a learned belief came from, kept beside the claim rather than
inside its statement. Nullable; rows learned under owner-source-learning@1
keep their embedded citation until the governed reshape supersedes them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d2e4f6a1c3"
down_revision: str | Sequence[str] | None = "a9c3e7f1b2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("memories", sa.Column("attribution_host", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("memories", "attribution_host")
