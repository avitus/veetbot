"""Repair conversation activity inflated by automatic memory audits (ADR-0167)."""

from collections.abc import Sequence

from alembic import op

revision: str = "f7c4a2d9e681"
down_revision: str | Sequence[str] | None = "e6b3d1a9c470"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # All appends lock/update the session before inserting their event. Exclude
    # those writers while deriving a repair so a concurrent reply cannot be lost.
    op.execute("LOCK TABLE sessions IN SHARE ROW EXCLUSIVE MODE")
    op.execute(
        """
        WITH activity AS (
            SELECT session_id,
                MAX(created_at) FILTER (WHERE
                    event_type IN ('memory.decayed', 'memory.retired')
                    AND actor_type = 'memory' AND run_id IS NULL
                ) AS maintenance_at,
                MAX(created_at) FILTER (WHERE NOT (
                    event_type IN ('memory.decayed', 'memory.retired')
                    AND actor_type = 'memory' AND run_id IS NULL
                )) AS conversation_at
            FROM events
            GROUP BY session_id
        )
        UPDATE sessions AS s
        SET updated_at = GREATEST(s.created_at, activity.conversation_at)
        FROM activity
        WHERE s.id = activity.session_id
          AND s.status = 'ACTIVE'
          AND s.updated_at = activity.maintenance_at
          AND s.updated_at > GREATEST(s.created_at, activity.conversation_at)
        """
    )


def downgrade() -> None:
    # The data repair is intentionally retained: no schema was changed, and
    # inventing fresh activity on rollback would reintroduce the reported bug.
    pass
