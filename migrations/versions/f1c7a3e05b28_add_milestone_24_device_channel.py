"""Add Milestone 24 — device capabilities, invocations, ingest, and triage sessions.

Revision ID: f1c7a3e05b28
Revises: e3a1c5d7f9b2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f1c7a3e05b28"
down_revision: str | Sequence[str] | None = "e3a1c5d7f9b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MUTED_KINDS_WITHOUT_DEVICE_INVOCATION = (
    "jsonb_typeof(muted_kinds) = 'array' AND muted_kinds <@ "
    '\'["approval_requested","question_asked","run_failed",'
    '"schedule_run_finished","schedule_occurrence_skipped",'
    '"ops_alert","ops_recovered","test"]\'::jsonb'
)
_MUTED_KINDS_WITH_DEVICE_INVOCATION = (
    "jsonb_typeof(muted_kinds) = 'array' AND muted_kinds <@ "
    '\'["approval_requested","question_asked","run_failed",'
    '"schedule_run_finished","schedule_occurrence_skipped",'
    '"ops_alert","ops_recovered","test","device_invocation"]\'::jsonb'
)
_NOTIFICATION_KINDS_WITHOUT_DEVICE_INVOCATION = (
    "kind IN ('approval_requested','question_asked','run_failed',"
    "'schedule_run_finished','schedule_occurrence_skipped','ops_alert',"
    "'ops_recovered','test')"
)
_NOTIFICATION_KINDS_WITH_DEVICE_INVOCATION = (
    "kind IN ('approval_requested','question_asked','run_failed',"
    "'schedule_run_finished','schedule_occurrence_skipped','ops_alert',"
    "'ops_recovered','test','device_invocation')"
)


def _tenant_policy(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_tenant_isolation ON {table} "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, condition)


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column(
            "capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_devices_device_capabilities_closed"),
        "devices",
        "jsonb_typeof(capabilities) = 'array' AND capabilities <@ '[\"device.sms.send\"]'::jsonb",
    )
    op.create_check_constraint(
        op.f("ck_devices_device_surface_capabilities"),
        "devices",
        "kind <> 'surface' OR capabilities = '[]'::jsonb",
    )
    _replace_check(
        "devices",
        "ck_devices_device_muted_kinds_closed",
        _MUTED_KINDS_WITH_DEVICE_INVOCATION,
    )
    _replace_check(
        "notification_outbox",
        "ck_notification_outbox_notification_kind_closed",
        _NOTIFICATION_KINDS_WITH_DEVICE_INVOCATION,
    )
    op.create_table(
        "device_invocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','sent','cancelled','failed','expired')",
            name=op.f("ck_device_invocations_status_closed"),
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            ondelete="CASCADE",
            name=op.f("fk_device_invocations_device_id_devices"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            ondelete="CASCADE",
            name=op.f("fk_device_invocations_run_id_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_invocations")),
    )
    op.create_index(
        "ix_device_invocations_device_status_created",
        "device_invocations",
        ["device_id", "status", "created_at"],
    )
    op.create_table(
        "device_ingest_receipts",
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=64), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            ondelete="CASCADE",
            name=op.f("fk_device_ingest_receipts_device_id_devices"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            ondelete="SET NULL",
            name=op.f("fk_device_ingest_receipts_run_id_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="SET NULL",
            name=op.f("fk_device_ingest_receipts_session_id_sessions"),
        ),
        sa.PrimaryKeyConstraint(
            "device_id",
            "channel",
            "digest",
            name=op.f("pk_device_ingest_receipts"),
        ),
    )
    op.create_table(
        "device_triage_sessions",
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            ondelete="CASCADE",
            name=op.f("fk_device_triage_sessions_device_id_devices"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
            name=op.f("fk_device_triage_sessions_session_id_sessions"),
        ),
        sa.PrimaryKeyConstraint(
            "device_id",
            "channel",
            name=op.f("pk_device_triage_sessions"),
        ),
    )
    _tenant_policy("device_invocations")
    _tenant_policy("device_ingest_receipts")
    _tenant_policy("device_triage_sessions")


def downgrade() -> None:
    op.drop_table("device_triage_sessions")
    op.drop_table("device_ingest_receipts")
    op.drop_index(
        "ix_device_invocations_device_status_created",
        table_name="device_invocations",
    )
    op.drop_table("device_invocations")
    _replace_check(
        "notification_outbox",
        "ck_notification_outbox_notification_kind_closed",
        _NOTIFICATION_KINDS_WITHOUT_DEVICE_INVOCATION,
    )
    _replace_check(
        "devices",
        "ck_devices_device_muted_kinds_closed",
        _MUTED_KINDS_WITHOUT_DEVICE_INVOCATION,
    )
    op.drop_constraint(op.f("ck_devices_device_surface_capabilities"), "devices", type_="check")
    op.drop_constraint(op.f("ck_devices_device_capabilities_closed"), "devices", type_="check")
    op.drop_column("devices", "capabilities")
