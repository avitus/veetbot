"""Add Milestone 4 policy, approval, and scope persistence.

Revision ID: e7c4a91f20b6
Revises: d4b6c21a8f03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e7c4a91f20b6"
down_revision: str | Sequence[str] | None = "d4b6c21a8f03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "principal_scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "tool_invocations",
        sa.Column("side_effect", sa.Text(), server_default=sa.text("'none'"), nullable=False),
    )
    op.add_column(
        "tool_invocations",
        sa.Column("risk", sa.Text(), server_default=sa.text("'low'"), nullable=False),
    )
    op.add_column(
        "tool_invocations",
        sa.Column("effective_arguments_hash", sa.Text(), nullable=True),
    )
    op.add_column(
        "tool_invocations",
        sa.Column("policy_decision", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "tool_invocations",
        sa.Column("structured_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("approvals", sa.Column("tenant_id", sa.Text(), nullable=True))
    op.add_column("approvals", sa.Column("principal_id", sa.Text(), nullable=True))
    op.add_column("approvals", sa.Column("session_id", sa.UUID(), nullable=True))
    op.add_column("approvals", sa.Column("action_kind", sa.Text(), nullable=True))
    op.add_column("approvals", sa.Column("action_id", sa.UUID(), nullable=True))
    op.add_column("approvals", sa.Column("risk", sa.Text(), nullable=True))
    op.add_column("approvals", sa.Column("policy_version", sa.Text(), nullable=True))
    op.add_column("approvals", sa.Column("revalidated_policy_version", sa.Text(), nullable=True))
    # Milestone 2 never creates approval rows; nullable staging keeps downgrade/upgrade
    # valid for databases produced before the M4 writer exists.
    op.alter_column("approvals", "tenant_id", nullable=False)
    op.alter_column("approvals", "principal_id", nullable=False)
    op.alter_column("approvals", "session_id", nullable=False)
    op.alter_column("approvals", "action_kind", nullable=False)
    op.alter_column("approvals", "action_id", nullable=False)
    op.alter_column("approvals", "risk", nullable=False)
    op.alter_column("approvals", "policy_version", nullable=False)
    op.create_index(
        "ix_approvals_tenant_status_created",
        "approvals",
        ["tenant_id", "status", "created_at"],
    )
    op.create_index("ix_approvals_run_id", "approvals", ["run_id"])
    op.create_index(
        "ix_approvals_pending_expiry",
        "approvals",
        ["status", "expires_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index("uq_approvals_action_id", "approvals", ["action_id"], unique=True)
    op.create_table(
        "policy_profiles",
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("profile_name", sa.Text(), nullable=False),
        sa.Column("profile_sha256", sa.Text(), nullable=False),
        sa.Column("hardline_sha256", sa.Text(), nullable=False),
        sa.Column("rule_count", sa.Integer(), nullable=False),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("loaded_by", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("policy_version", name=op.f("pk_policy_profiles")),
    )


def downgrade() -> None:
    op.drop_table("policy_profiles")
    op.drop_index("uq_approvals_action_id", table_name="approvals")
    op.drop_index("ix_approvals_pending_expiry", table_name="approvals")
    op.drop_index("ix_approvals_run_id", table_name="approvals")
    op.drop_index("ix_approvals_tenant_status_created", table_name="approvals")
    op.drop_column("approvals", "revalidated_policy_version")
    op.drop_column("approvals", "policy_version")
    op.drop_column("approvals", "risk")
    op.drop_column("approvals", "action_id")
    op.drop_column("approvals", "action_kind")
    op.drop_column("approvals", "session_id")
    op.drop_column("approvals", "principal_id")
    op.drop_column("approvals", "tenant_id")
    op.drop_column("tool_invocations", "structured_result")
    op.drop_column("tool_invocations", "policy_decision")
    op.drop_column("tool_invocations", "effective_arguments_hash")
    op.drop_column("tool_invocations", "risk")
    op.drop_column("tool_invocations", "side_effect")
    op.drop_column("runs", "principal_scopes")
