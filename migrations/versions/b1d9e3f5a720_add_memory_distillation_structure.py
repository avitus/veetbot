"""Add integrated episodes and adaptive memory lifecycle columns.

Revision ID: b1d9e3f5a720
Revises: a9c5e2f7d413
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b1d9e3f5a720"
down_revision: str | Sequence[str] | None = "a9c5e2f7d413"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("memories", sa.Column("claim_kind", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("derivation", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("longevity", sa.Text(), nullable=True))
    op.add_column(
        "memories", sa.Column("last_evidence_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("memories", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("memories", sa.Column("evidence_count", sa.Integer(), nullable=True))
    op.add_column("memories", sa.Column("lifecycle_policy_version", sa.Text(), nullable=True))
    op.add_column(
        "consolidation_runs",
        sa.Column(
            "decision_counts",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "consolidation_runs",
        sa.Column("episode_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "consolidation_runs",
        sa.Column("provider_call_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "consolidation_runs",
        sa.Column(
            "fallback_stages",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )

    op.create_table(
        "integrated_episodes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("source_event_ids", postgresql.JSONB(), nullable=False),
        sa.Column("source_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("narrative", sa.Text(), nullable=False),
        sa.Column("subjects", postgresql.JSONB(), nullable=False),
        sa.Column("integration_policy_version", sa.Text(), nullable=False),
        sa.Column("derivation_key", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
            name=op.f("fk_integrated_episodes_session_id_sessions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integrated_episodes")),
        sa.UniqueConstraint(
            "tenant_id",
            "principal_id",
            "derivation_key",
            name="uq_integrated_episodes_owner_derivation",
        ),
    )
    op.create_index(
        "ix_integrated_episodes_owner_session_source",
        "integrated_episodes",
        ["tenant_id", "principal_id", "session_id", "source_started_at"],
    )
    op.execute("ALTER TABLE integrated_episodes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE integrated_episodes FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY integrated_episodes_tenant_isolation ON integrated_episodes "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def downgrade() -> None:
    op.drop_table("integrated_episodes")
    for column in (
        "fallback_stages",
        "provider_call_count",
        "episode_count",
        "decision_counts",
    ):
        op.drop_column("consolidation_runs", column)
    for column in (
        "lifecycle_policy_version",
        "evidence_count",
        "last_used_at",
        "last_evidence_at",
        "longevity",
        "derivation",
        "claim_kind",
    ):
        op.drop_column("memories", column)
