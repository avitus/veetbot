"""Backfill immutable memory insertion order with admissions stopped."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ea3210a1b002"
down_revision: str | Sequence[str] | None = "ea3210a1b001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("LOCK TABLE memories, memory_revisions IN SHARE ROW EXCLUSIVE MODE")
    op.execute("""
        WITH ordered AS (
            SELECT id, row_number() OVER (
                PARTITION BY tenant_id, principal_id ORDER BY created_at, id
            ) AS sequence
            FROM memories
        )
        UPDATE memories SET creation_sequence = ordered.sequence FROM ordered
        WHERE memories.id = ordered.id AND memories.creation_sequence IS NULL
    """)
    op.execute("""
        INSERT INTO reconsolidation_owners (tenant_id, principal_id, creation_count, change_count)
        SELECT tenant_id, principal_id, max(creation_sequence), max(creation_sequence) FROM memories
        GROUP BY tenant_id, principal_id ON CONFLICT DO NOTHING
    """)
    op.execute("""
        INSERT INTO reconsolidation_changes (
            tenant_id, principal_id, sequence, belief_id, content_revision,
            creation_sequence, reason
        )
        SELECT tenant_id, principal_id, creation_sequence, id, content_revision, creation_sequence,
            CASE WHEN erasure_pending THEN 'erased' ELSE 'created' END
        FROM memories ON CONFLICT DO NOTHING
    """)
    op.execute("""
        UPDATE memory_revisions SET creation_sequence = memories.creation_sequence
        FROM memories WHERE memory_revisions.belief_id = memories.id
        AND memory_revisions.creation_sequence IS NULL
    """)


def downgrade() -> None:
    # Serialize the history check with owner admission before any destructive DDL.
    op.execute("LOCK TABLE reconsolidation_owners, reconsolidation_jobs IN ACCESS EXCLUSIVE MODE")
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM reconsolidation_jobs)"))
        .scalar()
    ):
        raise NotImplementedError(
            "Disable reconsolidation and export its history before downgrade; "
            "reversing source sequences would lose scan and revision identity"
        )
    # These additive values disappear with the structural migration. Original
    # records, evidence clocks and recall positions need no reverse data rewrite.
