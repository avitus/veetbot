"""Add owner-scoped reconsolidation maintenance persistence."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

revision: str = "ea3210a1b001"
down_revision: str | Sequence[str] | None = "f7c4a2d9e681"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reconsolidation_owners",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("creation_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("change_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("current_job_id", sa.UUID(), nullable=True),
        sa.PrimaryKeyConstraint(
            "tenant_id", "principal_id", name=op.f("pk_reconsolidation_owners")
        ),
    )
    op.create_table(
        "reconsolidation_changes",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("belief_id", sa.UUID(), nullable=False),
        sa.Column("content_revision", sa.BigInteger(), nullable=False),
        sa.Column("creation_sequence", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["reconsolidation_owners.tenant_id", "reconsolidation_owners.principal_id"],
            name=op.f("fk_reconsolidation_changes_tenant_id_reconsolidation_owners"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "principal_id", "sequence", name=op.f("pk_reconsolidation_changes")
        ),
    )
    op.create_table(
        "reconsolidation_days",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column(
            "charged_usd",
            sa.Numeric(precision=20, scale=10),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "charged_usd >= 0 AND charged_usd <= 2",
            name=op.f("ck_reconsolidation_days_recon_day_budget"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["reconsolidation_owners.tenant_id", "reconsolidation_owners.principal_id"],
            name=op.f("fk_reconsolidation_days_tenant_id_reconsolidation_owners"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "principal_id", "day", name=op.f("pk_reconsolidation_days")
        ),
    )
    op.create_table(
        "reconsolidation_jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("policy", sa.Text(), nullable=False),
        sa.Column("due_day", sa.Date(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("full_bound", sa.BigInteger(), nullable=False),
        sa.Column("change_bound", sa.BigInteger(), nullable=False),
        sa.Column("full_cursor", sa.BigInteger(), nullable=False),
        sa.Column("change_cursor", sa.BigInteger(), nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=False),
        sa.Column("lease_token", sa.UUID(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("slice_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("slice_day", sa.Date(), nullable=False),
        sa.Column("slice_spent", sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.Column("lease_expirations", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "slice_spent >= 0 AND slice_spent <= 0.25 AND requests BETWEEN 0 AND 2",
            name=op.f("ck_reconsolidation_jobs_recon_slice_budget"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["reconsolidation_owners.tenant_id", "reconsolidation_owners.principal_id"],
            name=op.f("fk_reconsolidation_jobs_tenant_id_reconsolidation_owners"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconsolidation_jobs")),
        sa.UniqueConstraint(
            "tenant_id", "principal_id", "due_day", "policy", name="uq_recon_jobs_due"
        ),
        sa.UniqueConstraint(
            "tenant_id", "principal_id", "id", name="uq_reconsolidation_jobs_owner_id"
        ),
    )
    op.create_index(
        "ix_recon_jobs_due", "reconsolidation_jobs", ["state", "lease_expires_at"], unique=False
    )
    op.create_table(
        "reconsolidation_audits",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("lease_token", sa.UUID(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("inspected", sa.Integer(), nullable=False),
        sa.Column("excluded", sa.Integer(), nullable=False),
        sa.Column("selected_groups", sa.Integer(), nullable=False),
        sa.Column("not_selected", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id", "job_id"],
            [
                "reconsolidation_jobs.tenant_id",
                "reconsolidation_jobs.principal_id",
                "reconsolidation_jobs.id",
            ],
            name=op.f("fk_reconsolidation_audits_tenant_id_reconsolidation_jobs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "principal_id", "lease_token", name=op.f("pk_reconsolidation_audits")
        ),
    )
    op.create_table(
        "reconsolidation_groups",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("policy", sa.Text(), nullable=False),
        sa.Column("sources", postgresql.JSONB(astext_type=Text()), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.UUID(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "attempts BETWEEN 0 AND 3", name=op.f("ck_reconsolidation_groups_recon_group_attempts")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id", "job_id"],
            [
                "reconsolidation_jobs.tenant_id",
                "reconsolidation_jobs.principal_id",
                "reconsolidation_jobs.id",
            ],
            name=op.f("fk_reconsolidation_groups_tenant_id_reconsolidation_jobs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconsolidation_groups")),
        sa.UniqueConstraint(
            "tenant_id", "principal_id", "id", name="uq_reconsolidation_groups_owner_id"
        ),
        sa.UniqueConstraint(
            "tenant_id", "principal_id", "policy", "input_digest", name="uq_recon_groups_input"
        ),
    )
    op.create_index(
        "ix_recon_groups_pending",
        "reconsolidation_groups",
        ["tenant_id", "principal_id", "state", "created_at", "id"],
        unique=False,
    )
    op.create_table(
        "reconsolidation_spend",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("lease_token", sa.UUID(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("maximum_usd", sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column("charged_usd", sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "maximum_usd > 0 AND maximum_usd <= 0.25 "
            "AND charged_usd >= 0 AND charged_usd <= maximum_usd",
            name=op.f("ck_reconsolidation_spend_recon_reservation_budget"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id", "job_id"],
            [
                "reconsolidation_jobs.tenant_id",
                "reconsolidation_jobs.principal_id",
                "reconsolidation_jobs.id",
            ],
            name=op.f("fk_reconsolidation_spend_tenant_id_reconsolidation_jobs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconsolidation_spend")),
        sa.UniqueConstraint(
            "tenant_id", "principal_id", "id", name="uq_reconsolidation_spend_owner_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "principal_id",
            "lease_token",
            "request_digest",
            name="uq_recon_spend_request",
        ),
    )
    op.create_index(
        "ix_recon_spend_day",
        "reconsolidation_spend",
        ["tenant_id", "principal_id", "day"],
        unique=False,
    )
    op.add_column(
        "memories",
        sa.Column("content_revision", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column("memories", sa.Column("creation_sequence", sa.BigInteger(), nullable=True))
    op.add_column(
        "memory_revisions",
        sa.Column("content_revision", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column(
        "memory_revisions", sa.Column("creation_sequence", sa.BigInteger(), nullable=True)
    )
    for table in (
        "reconsolidation_owners",
        "reconsolidation_changes",
        "reconsolidation_days",
        "reconsolidation_jobs",
        "reconsolidation_groups",
        "reconsolidation_spend",
        "reconsolidation_audits",
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON {table} "
            "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
            "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
        )


def downgrade() -> None:
    # Serialize the history check with owner admission before any destructive DDL.
    op.execute("LOCK TABLE reconsolidation_owners, reconsolidation_jobs IN ACCESS EXCLUSIVE MODE")
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM reconsolidation_jobs)"))
        .scalar()
    ):
        raise RuntimeError(
            "Disable reconsolidation and export its history before downgrade; "
            "jobs, audit history and undo would be lost"
        )
    op.drop_column("memory_revisions", "creation_sequence")
    op.drop_column("memory_revisions", "content_revision")
    op.drop_column("memories", "creation_sequence")
    op.drop_column("memories", "content_revision")
    op.drop_index("ix_recon_spend_day", table_name="reconsolidation_spend")
    op.drop_table("reconsolidation_spend")
    op.drop_index("ix_recon_groups_pending", table_name="reconsolidation_groups")
    op.drop_table("reconsolidation_groups")
    op.drop_table("reconsolidation_audits")
    op.drop_index("ix_recon_jobs_due", table_name="reconsolidation_jobs")
    op.drop_table("reconsolidation_jobs")
    op.drop_table("reconsolidation_days")
    op.drop_table("reconsolidation_changes")
    op.drop_table("reconsolidation_owners")
