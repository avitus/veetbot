"""Persist an owner-authorized forward-only dreaming boundary."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ea3210a1b00d"
down_revision: str | Sequence[str] | None = "ea3310a1b003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "reconsolidation_owners", sa.Column("forward_dreaming", postgresql.JSONB(), nullable=True)
    )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute("LOCK TABLE reconsolidation_owners IN ACCESS EXCLUSIVE MODE")
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM reconsolidation_owners "
                "WHERE forward_dreaming IS NOT NULL)"
            )
        )
        .scalar()
    ):
        raise RuntimeError("An activated forward dreaming boundary must not be removed")
    op.drop_column("reconsolidation_owners", "forward_dreaming")
