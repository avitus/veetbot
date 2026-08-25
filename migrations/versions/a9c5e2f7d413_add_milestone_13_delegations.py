"""Add Milestone 13 — delegation ledger and the sibling-check run index.

Revision ID: a9c5e2f7d413
Revises: e5c8a1d9f204
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a9c5e2f7d413"
down_revision: str | Sequence[str] | None = "e5c8a1d9f204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "delegations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("parent_run_id", sa.Uuid(), nullable=False),
        sa.Column("parent_session_id", sa.Uuid(), nullable=False),
        sa.Column("invocation_id", sa.Uuid(), nullable=False),
        sa.Column("depth", sa.SmallInteger(), nullable=False),
        sa.Column("brief", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("derived_limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("granted_scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("children", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("links_erased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "depth >= 0",
            name=op.f("ck_delegations_delegation_depth_nonnegative"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','JOINED','CANCELLED','REJECTED')",
            name=op.f("ck_delegations_delegation_status_closed"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(brief) = 'object' AND jsonb_typeof(children) = 'array' "
            "AND jsonb_typeof(derived_limits) = 'array' "
            "AND jsonb_typeof(granted_scopes) = 'array'",
            name=op.f("ck_delegations_delegation_documents_shaped"),
        ),
        sa.CheckConstraint(
            "(status = 'JOINED' AND joined_at IS NOT NULL AND result IS NOT NULL) OR "
            "(status <> 'JOINED' AND joined_at IS NULL AND result IS NULL)",
            name=op.f("ck_delegations_delegation_join_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["parent_run_id"],
            ["runs.id"],
            ondelete="CASCADE",
            name=op.f("fk_delegations_parent_run_id_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["parent_session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
            name=op.f("fk_delegations_parent_session_id_sessions"),
        ),
        sa.ForeignKeyConstraint(
            ["invocation_id"],
            ["tool_invocations.id"],
            ondelete="CASCADE",
            name=op.f("fk_delegations_invocation_id_tool_invocations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_delegations")),
        sa.UniqueConstraint("invocation_id", name="uq_delegations_invocation"),
    )
    op.create_index("ix_delegations_parent_run", "delegations", ["parent_run_id"])
    op.create_index(
        "ix_delegations_tenant_principal_created",
        "delegations",
        ["tenant_id", "principal_id", "created_at", "id"],
    )
    op.create_index(
        "ix_runs_parent_kind",
        "runs",
        ["parent_run_id", "run_kind"],
        postgresql_where=sa.text("parent_run_id IS NOT NULL"),
    )
    op.execute("ALTER TABLE delegations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE delegations FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY delegations_tenant_isolation ON delegations "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def downgrade() -> None:
    op.drop_index("ix_runs_parent_kind", table_name="runs")
    op.drop_table("delegations")
