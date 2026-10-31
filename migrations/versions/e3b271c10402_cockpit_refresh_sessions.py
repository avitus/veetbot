"""Keep existing widget refresh audit sessions out of the conversation index.

Revision ID: e3b271c10402
Revises: d8a268b10301
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e3b271c10402"
down_revision: str | Sequence[str] | None = "d8a268b10301"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Use each occurrence's immutable revision, not the schedule's current settings."""
    op.execute("SET LOCAL row_security = off")
    op.execute("""
        UPDATE sessions AS session
        SET metadata = session.metadata || '{"cockpit_refresh": true}'::jsonb
        FROM schedule_occurrences AS occurrence
        JOIN schedule_revisions AS revision
          ON revision.schedule_id = occurrence.schedule_id
         AND revision.revision = occurrence.schedule_revision
        JOIN schedules AS schedule ON schedule.id = occurrence.schedule_id
        WHERE session.id = occurrence.session_id
          AND session.tenant_id = schedule.tenant_id
          AND session.principal_id = schedule.principal_id
          AND revision.definition->'requested_scopes' ? 'cockpit.write'
    """)


def downgrade() -> None:
    """Remove only the presentation marker, retaining every audit record."""
    op.execute("SET LOCAL row_security = off")
    op.execute(
        "UPDATE sessions SET metadata = metadata - 'cockpit_refresh' "
        "WHERE metadata @> '{\"cockpit_refresh\": true}'::jsonb"
    )
