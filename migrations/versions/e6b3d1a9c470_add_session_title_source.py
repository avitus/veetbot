"""Record where a session's title came from and its pending title request (ADR-0155)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6b3d1a9c470"
down_revision: str | Sequence[str] | None = "d0143a76b201"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("title_source", sa.String(length=32), nullable=True))
    op.add_column(
        "sessions", sa.Column("title_requested_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Every feature that names a session at creation marks its metadata with one
    # of these keys; any other titled session took its title from the owner's
    # first message.
    op.execute(
        """
        UPDATE sessions
        SET title_source = CASE
            WHEN metadata ?| ARRAY[
                'email_thread_id', 'email_operational', 'schedule_id', 'run_kind',
                'device_triage', 'purpose'
            ] THEN 'fixed'
            ELSE 'first_message'
        END
        WHERE title IS NOT NULL
        """
    )
    op.create_check_constraint(
        "session_title_source",
        "sessions",
        "title_source IN ('first_message', 'generated', 'fixed')",
    )
    op.create_index(
        "ix_sessions_title_requested",
        "sessions",
        ["tenant_id", "principal_id", "title_requested_at"],
        postgresql_where=sa.text("title_requested_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sessions_title_requested",
        table_name="sessions",
        postgresql_where=sa.text("title_requested_at IS NOT NULL"),
    )
    op.drop_constraint(op.f("ck_sessions_session_title_source"), "sessions", type_="check")
    op.drop_column("sessions", "title_requested_at")
    op.drop_column("sessions", "title_source")
