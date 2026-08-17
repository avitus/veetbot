"""Index memory-formation event selection.

Revision ID: e8f2a4c6d810
Revises: d7e9f1a2b3c4
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e8f2a4c6d810"
down_revision: str | Sequence[str] | None = "d7e9f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_events_type_session_sequence",
        "events",
        ["event_type", "session_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_index("ix_events_type_session_sequence", table_name="events")
