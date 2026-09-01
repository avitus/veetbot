"""Backfill adaptive memory lifecycle fields and move the idle index.

Revision ID: c2e0f4a6b831
Revises: b1d9e3f5a720
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2e0f4a6b831"
down_revision: str | Sequence[str] | None = "b1d9e3f5a720"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            updated_rows integer;
        BEGIN
            LOOP
                WITH batch AS (
                    SELECT id
                    FROM memories
                    WHERE claim_kind IS NULL
                       OR derivation IS NULL
                       OR longevity IS NULL
                       OR last_evidence_at IS NULL
                       OR evidence_count IS NULL
                       OR lifecycle_policy_version IS NULL
                    ORDER BY id
                    LIMIT 1000
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE memories AS memory
                SET claim_kind = COALESCE(
                        memory.claim_kind,
                        CASE memory.belief_type
                            WHEN 'preference' THEN 'preference'
                            WHEN 'relationship' THEN 'relationship'
                            WHEN 'procedure_pointer' THEN 'resource'
                            ELSE 'project_fact'
                        END
                    ),
                    derivation = COALESCE(memory.derivation, 'direct'),
                    longevity = COALESCE(memory.longevity, 'durable'),
                    last_evidence_at = COALESCE(
                        memory.last_evidence_at, memory.valid_from
                    ),
                    evidence_count = COALESCE(
                        memory.evidence_count,
                        GREATEST(1, jsonb_array_length(memory.source_event_ids))
                    ),
                    lifecycle_policy_version = COALESCE(
                        memory.lifecycle_policy_version, 'lifecycle@1-backfill'
                    )
                FROM batch
                WHERE memory.id = batch.id;
                GET DIAGNOSTICS updated_rows = ROW_COUNT;
                EXIT WHEN updated_rows = 0;
            END LOOP;
        END
        $$
        """
    )
    for column in (
        "claim_kind",
        "derivation",
        "longevity",
        "last_evidence_at",
        "evidence_count",
        "lifecycle_policy_version",
    ):
        op.alter_column("memories", column, existing_type=_column_type(column), nullable=False)
    op.drop_index("ix_memories_principal_idle", table_name="memories")
    op.create_index(
        "ix_memories_principal_idle",
        "memories",
        ["tenant_id", "principal_id", "status", "last_evidence_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_memories_principal_idle", table_name="memories")
    op.create_index(
        "ix_memories_principal_idle",
        "memories",
        ["tenant_id", "principal_id", "status", "last_reinforced_at"],
    )
    for column in (
        "claim_kind",
        "derivation",
        "longevity",
        "last_evidence_at",
        "evidence_count",
        "lifecycle_policy_version",
    ):
        op.alter_column("memories", column, existing_type=_column_type(column), nullable=True)


def _column_type(column: str) -> sa.types.TypeEngine:
    return (
        sa.Integer()
        if column == "evidence_count"
        else (sa.DateTime(timezone=True) if column == "last_evidence_at" else sa.Text())
    )
