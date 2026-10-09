"""Persist reversible reconsolidation merges and hash-only undo receipts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ea3210a1b006"
down_revision: str | Sequence[str] | None = "ea3210a1b005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for column in ("claimed_groups", "operations"):
        op.add_column(
            "reconsolidation_jobs",
            sa.Column(column, sa.Integer(), nullable=False, server_default=sa.text("0")),
        )
    op.create_check_constraint(
        "recon_slice_work",
        "reconsolidation_jobs",
        "claimed_groups BETWEEN 0 AND 4 AND operations BETWEEN 0 AND 8",
    )
    op.create_table(
        "reconsolidation_operations",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("group_id", sa.UUID(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("store_position", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "principal_id", "id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id", "group_id"],
            [
                "reconsolidation_groups.tenant_id",
                "reconsolidation_groups.principal_id",
                "reconsolidation_groups.id",
            ],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "tenant_id", "principal_id", "group_id", name="uq_recon_operation_group"
        ),
        sa.CheckConstraint("revision > 0 AND store_position > 0", name="recon_operation_revision"),
        sa.CheckConstraint(
            "state IN ('committed', 'invalidated', 'undone')", name="recon_operation_state"
        ),
    )
    op.create_index(
        "ix_recon_operation_page",
        "reconsolidation_operations",
        ["tenant_id", "principal_id", "created_at", "id"],
    )
    op.create_table(
        "reconsolidation_dependencies",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.UUID(), nullable=False),
        sa.Column("belief_id", sa.UUID(), nullable=False),
        sa.Column("content_revision", sa.BigInteger(), nullable=False),
        sa.Column("source_session_id", sa.UUID(), nullable=False),
        sa.Column("source_event_ids", postgresql.JSONB(), nullable=False),
        sa.Column("evidence_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "principal_id", "operation_id", "belief_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id", "operation_id"],
            [
                "reconsolidation_operations.tenant_id",
                "reconsolidation_operations.principal_id",
                "reconsolidation_operations.id",
            ],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_recon_dependency_source",
        "reconsolidation_dependencies",
        ["tenant_id", "principal_id", "belief_id"],
    )
    op.create_table(
        "reconsolidation_members",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.UUID(), nullable=False),
        sa.Column("member_id", sa.UUID(), nullable=False),
        sa.Column("canonical_id", sa.UUID(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "principal_id", "operation_id", "member_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id", "operation_id"],
            [
                "reconsolidation_operations.tenant_id",
                "reconsolidation_operations.principal_id",
                "reconsolidation_operations.id",
            ],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "uq_recon_active_member",
        "reconsolidation_members",
        ["tenant_id", "principal_id", "member_id"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_table(
        "reconsolidation_blocks",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "principal_id", "signature"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["reconsolidation_owners.tenant_id", "reconsolidation_owners.principal_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "reconsolidation_undo",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("key_digest", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.UUID(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "principal_id", "key_digest"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id", "operation_id"],
            [
                "reconsolidation_operations.tenant_id",
                "reconsolidation_operations.principal_id",
                "reconsolidation_operations.id",
            ],
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "reconsolidation_operation_history",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.UUID(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "principal_id", "operation_id", "revision"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id", "operation_id"],
            [
                "reconsolidation_operations.tenant_id",
                "reconsolidation_operations.principal_id",
                "reconsolidation_operations.id",
            ],
            ondelete="CASCADE",
        ),
    )
    for table in (
        "reconsolidation_operations",
        "reconsolidation_dependencies",
        "reconsolidation_members",
        "reconsolidation_blocks",
        "reconsolidation_undo",
        "reconsolidation_operation_history",
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON {table} "
            "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
            "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
        )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute("LOCK TABLE reconsolidation_owners, reconsolidation_jobs IN ACCESS EXCLUSIVE MODE")
    if (
        op.get_bind()
        .execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM reconsolidation_jobs WHERE state = 'running')")
        )
        .scalar()
    ):
        raise RuntimeError(
            "Disable reconsolidation, finish leased work and export its history before downgrade; "
            "derived merge and undo history would be lost"
        )
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM reconsolidation_operations)"))
        .scalar()
    ):
        raise RuntimeError(
            "Export and explicitly remove derived merge history before downgrade; "
            "undo history would be lost"
        )
    op.drop_table("reconsolidation_operation_history")
    op.drop_table("reconsolidation_undo")
    op.drop_table("reconsolidation_blocks")
    op.drop_table("reconsolidation_members")
    op.drop_table("reconsolidation_dependencies")
    op.drop_table("reconsolidation_operations")
    op.drop_constraint("recon_slice_work", "reconsolidation_jobs", type_="check")
    op.drop_column("reconsolidation_jobs", "operations")
    op.drop_column("reconsolidation_jobs", "claimed_groups")
