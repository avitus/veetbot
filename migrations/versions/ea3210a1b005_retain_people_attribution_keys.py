"""Retain opaque attribution keys after People payload erasure."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ea3210a1b005"
down_revision: str | Sequence[str] | None = "ea3210a1b004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Require an unfiltered view; a restricted role must fail rather than
    # silently backfilling only the rows its tenant policy happens to expose.
    op.execute("SET LOCAL row_security = off")
    op.add_column(
        "people_heads", sa.Column("memory_attribution", postgresql.JSONB(), nullable=True)
    )
    # The head DDL lock also serializes this backfill with People writers. NULL
    # means an older purge destroyed the metadata; never guess an empty footprint.
    op.execute("""
        UPDATE people_heads h SET memory_attribution = CASE h.kind
            WHEN 'memory_link' THEN jsonb_build_object('belief_id', r.payload->'belief_id')
            WHEN 'mention' THEN jsonb_build_object('source_id', r.payload->'source_id')
            WHEN 'source' THEN jsonb_build_object('source_id', h.id::text,
                'session_id', r.payload->'session_id',
                'event_sequence', r.payload->'event_sequence')
            ELSE '{}'::jsonb END
        FROM people_revisions r
        WHERE (h.tenant_id,h.principal_id,h.id,h.revision) =
              (r.tenant_id,r.principal_id,r.entity_id,r.revision)
    """)
    op.create_check_constraint(
        "people_attribution_object",
        "people_heads",
        "memory_attribution IS NULL OR jsonb_typeof(memory_attribution) = 'object'",
    )
    op.create_index(
        "ix_people_attribution_belief",
        "people_heads",
        ["tenant_id", "principal_id", sa.text("(memory_attribution->>'belief_id')")],
    )
    op.create_index(
        "ix_people_attribution_source",
        "people_heads",
        ["tenant_id", "principal_id", sa.text("(memory_attribution->>'source_id')")],
    )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute(
        "LOCK TABLE reconsolidation_owners, reconsolidation_jobs, people_heads "
        "IN ACCESS EXCLUSIVE MODE"
    )
    if (
        op.get_bind()
        .execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM reconsolidation_jobs WHERE state = 'running')")
        )
        .scalar()
    ):
        raise RuntimeError(
            "Disable reconsolidation, finish leased work and export its history before downgrade; "
            "attribution history would be lost"
        )
    op.execute(
        "DO $$ BEGIN RAISE NOTICE 'Removing reconsolidation attribution keys; "
        "purged attribution history will be unavailable'; END $$"
    )
    op.drop_index("ix_people_attribution_source", table_name="people_heads")
    op.drop_index("ix_people_attribution_belief", table_name="people_heads")
    op.drop_constraint("people_attribution_object", "people_heads", type_="check")
    op.drop_column("people_heads", "memory_attribution")
