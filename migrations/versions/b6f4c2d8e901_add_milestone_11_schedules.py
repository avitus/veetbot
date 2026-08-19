"""Add Milestone 11 schedule definitions and occurrence history.

Revision ID: b6f4c2d8e901
Revises: a4c8e2f6b913
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b6f4c2d8e901"
down_revision: str | Sequence[str] | None = "d2a6f8b1c304"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tenant_policy(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_tenant_isolation ON {table} "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def _schedule_child_policy(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_tenant_isolation ON {table} "
        f"USING (EXISTS (SELECT 1 FROM schedules s WHERE s.id = {table}.schedule_id)) "
        f"WITH CHECK (EXISTS (SELECT 1 FROM schedules s WHERE s.id = {table}.schedule_id))"
    )


def upgrade() -> None:
    op.create_table(
        "schedules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("pause_reason", sa.Text(), nullable=True),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('ACTIVE','PAUSED','COMPLETED','CANCELLED')",
            name=op.f("ck_schedules_schedule_state_values"),
        ),
        sa.CheckConstraint(
            "(state = 'PAUSED' AND pause_reason IN ('user','failure_limit')) OR "
            "(state <> 'PAUSED' AND pause_reason IS NULL)",
            name=op.f("ck_schedules_schedule_pause_reason_consistency"),
        ),
        sa.CheckConstraint(
            "current_revision > 0", name=op.f("ck_schedules_schedule_revision_positive")
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name=op.f("ck_schedules_schedule_failures_nonnegative"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('COMPLETED','CANCELLED') OR next_fire_at IS NULL",
            name=op.f("ck_schedules_schedule_terminal_not_due"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schedules")),
    )
    op.create_index("ix_schedules_due", "schedules", ["state", "next_fire_at"])
    op.create_index(
        "ix_schedules_tenant_principal_updated",
        "schedules",
        ["tenant_id", "principal_id", "updated_at", "id"],
    )
    op.create_table(
        "schedule_revisions",
        sa.Column("schedule_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("definition", postgresql.JSONB(), nullable=False),
        sa.Column("created_by_principal_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["schedules.id"],
            ondelete="RESTRICT",
            name=op.f("fk_schedule_revisions_schedule_id_schedules"),
        ),
        sa.PrimaryKeyConstraint("schedule_id", "revision", name=op.f("pk_schedule_revisions")),
    )
    op.create_table(
        "schedule_occurrences",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("schedule_id", sa.Uuid(), nullable=False),
        sa.Column("schedule_revision", sa.Integer(), nullable=False),
        sa.Column("nominal_fire_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disposition", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("authority_version", sa.Text(), nullable=True),
        sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("links_erased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "disposition IN ('MATERIALIZED','MISSED','SKIPPED_OVERLAP',"
            "'AUTHORIZATION_FAILED','CONFIGURATION_FAILED')",
            name=op.f("ck_schedule_occurrences_schedule_occurrence_disposition_values"),
        ),
        sa.CheckConstraint(
            "(disposition = 'MATERIALIZED' AND authority_version IS NOT NULL "
            "AND materialized_at IS NOT NULL AND reason_code IS NULL "
            "AND materialized_at >= nominal_fire_at AND ((links_erased_at IS NULL "
            "AND session_id IS NOT NULL AND run_id IS NOT NULL) OR "
            "(links_erased_at IS NOT NULL AND session_id IS NULL AND run_id IS NULL "
            "AND links_erased_at >= materialized_at))) OR "
            "(disposition <> 'MATERIALIZED' AND session_id IS NULL AND run_id IS NULL "
            "AND materialized_at IS NULL AND links_erased_at IS NULL "
            "AND reason_code IS NOT NULL)",
            name=op.f("ck_schedule_occurrences_schedule_occurrence_links_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id", "schedule_revision"],
            ["schedule_revisions.schedule_id", "schedule_revisions.revision"],
            ondelete="RESTRICT",
            name="fk_schedule_occurrences_revision",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            deferrable=True,
            initially="DEFERRED",
            ondelete="SET NULL",
            name=op.f("fk_schedule_occurrences_session_id_sessions"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            deferrable=True,
            initially="DEFERRED",
            ondelete="SET NULL",
            name=op.f("fk_schedule_occurrences_run_id_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schedule_occurrences")),
        sa.UniqueConstraint(
            "schedule_id", "nominal_fire_at", name="uq_schedule_occurrence_nominal"
        ),
    )
    op.create_index(
        "ix_schedule_occurrences_history",
        "schedule_occurrences",
        ["schedule_id", "nominal_fire_at"],
    )
    op.create_index(
        "uq_schedule_occurrences_run_id",
        "schedule_occurrences",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("run_id IS NOT NULL"),
    )
    op.create_table(
        "schedule_idempotency_keys",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("schedule_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["schedules.id"],
            ondelete="RESTRICT",
            name=op.f("fk_schedule_idempotency_keys_schedule_id_schedules"),
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "principal_id", "key", name=op.f("pk_schedule_idempotency_keys")
        ),
    )
    _tenant_policy("schedules")
    _tenant_policy("schedule_idempotency_keys")
    _schedule_child_policy("schedule_revisions")
    _schedule_child_policy("schedule_occurrences")


def downgrade() -> None:
    op.drop_table("schedule_idempotency_keys")
    op.drop_table("schedule_occurrences")
    op.drop_table("schedule_revisions")
    op.drop_table("schedules")
