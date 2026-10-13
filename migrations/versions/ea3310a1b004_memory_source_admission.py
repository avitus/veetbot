"""Default-denied owner source admission; original event payloads are unchanged."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ea3310a1b004"
down_revision: str | Sequence[str] | None = "ea3310a1b003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("events", sa.Column("memory_source_sha256", sa.Text(), nullable=True))
    op.add_column(
        "events",
        sa.Column("memory_source_blocked", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    # Older code cannot recover source messages. On a later re-upgrade every
    # existing event is unknown again; losing stamps never grants access.
    op.drop_column("events", "memory_source_blocked")
    op.drop_column("events", "memory_source_sha256")
