"""Add Milestones 14 and 25 surface persistence.

Revision ID: d8f3a1c7b205
Revises: c9e2a7f4b106
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d8f3a1c7b205"
down_revision: str | Sequence[str] | None = "c9e2a7f4b106"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEVICE_PUSH_PROVIDERS_BEFORE = "push_provider IS NULL OR push_provider IN ('apns','telegram')"
_DEVICE_PUSH_PROVIDERS_AFTER = (
    "push_provider IS NULL OR push_provider IN ('apns','telegram','whatsapp')"
)
_DEVICE_SURFACE_ROUTING_BEFORE = (
    "(kind = 'surface' AND (push_provider IS NULL OR push_provider = 'telegram')) OR "
    "(kind <> 'surface' AND (push_provider IS NULL OR push_provider <> 'telegram'))"
)
_DEVICE_SURFACE_ROUTING_AFTER = (
    "(kind = 'surface' AND "
    "(push_provider IS NULL OR push_provider IN ('telegram','whatsapp'))) OR "
    "(kind <> 'surface' AND "
    "(push_provider IS NULL OR push_provider NOT IN ('telegram','whatsapp')))"
)


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, condition)


def _tenant_policy(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_tenant_isolation ON {table} "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def _surface_policy(table: str) -> None:
    predicate = (
        f"EXISTS (SELECT 1 FROM devices WHERE devices.id = {table}.surface_id "
        "AND devices.tenant_id = current_setting('agent_core.tenant_id', true))"
    )
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_tenant_isolation ON {table} "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def upgrade() -> None:
    _replace_check(
        "devices",
        "ck_devices_device_push_provider_closed",
        _DEVICE_PUSH_PROVIDERS_AFTER,
    )
    _replace_check(
        "devices",
        "ck_devices_device_surface_routing",
        _DEVICE_SURFACE_ROUTING_AFTER,
    )
    op.create_table(
        "surface_pairing_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("surface_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("code_hash", sa.LargeBinary(), nullable=False),
        sa.Column("code_salt", sa.LargeBinary(), nullable=False),
        sa.Column("granted_scopes", postgresql.JSONB(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_principal_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "max_attempts > 0",
            name=op.f("ck_surface_pairing_codes_surface_pairing_code_max_attempts_positive"),
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND attempts <= max_attempts",
            name=op.f("ck_surface_pairing_codes_surface_pairing_code_attempts_bounded"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(granted_scopes) = 'array'",
            name=op.f("ck_surface_pairing_codes_surface_pairing_code_scopes_array"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_surface_pairing_codes_surface_pairing_code_expiry_after_creation"),
        ),
        sa.ForeignKeyConstraint(
            ["surface_id"],
            ["devices.id"],
            ondelete="CASCADE",
            name=op.f("fk_surface_pairing_codes_surface_id_devices"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_surface_pairing_codes")),
    )
    op.create_table(
        "surface_pairings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("surface_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("sender_id", sa.Text(), nullable=False),
        sa.Column("sender_label", sa.Text(), nullable=True),
        sa.Column("granted_scopes", postgresql.JSONB(), nullable=False),
        sa.Column("paired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.Text(), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "jsonb_typeof(granted_scopes) = 'array'",
            name=op.f("ck_surface_pairings_surface_pairing_scopes_array"),
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL AND revoked_by IS NULL) OR "
            "(revoked_at IS NOT NULL AND revoked_by IS NOT NULL)",
            name=op.f("ck_surface_pairings_surface_pairing_revocation_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["surface_id"],
            ["devices.id"],
            ondelete="CASCADE",
            name=op.f("fk_surface_pairings_surface_id_devices"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_surface_pairings")),
    )
    op.create_index(
        "uq_surface_pairings_live_sender",
        "surface_pairings",
        ["surface_id", "sender_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "surface_sender_lockouts",
        sa.Column("surface_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sender_id", sa.Text(), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "failed_attempts >= 0",
            name=op.f("ck_surface_sender_lockouts_surface_lockout_attempts_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["surface_id"],
            ["devices.id"],
            ondelete="CASCADE",
            name=op.f("fk_surface_sender_lockouts_surface_id_devices"),
        ),
        sa.PrimaryKeyConstraint(
            "surface_id",
            "sender_id",
            name=op.f("pk_surface_sender_lockouts"),
        ),
    )
    op.create_table(
        "surface_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("surface_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("external_key", sa.Text(), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "rotated_at IS NULL OR rotated_at >= created_at",
            name=op.f("ck_surface_sessions_surface_session_rotation_consistent"),
        ),
        sa.CheckConstraint(
            "last_inbound_at IS NULL OR last_inbound_at >= created_at",
            name=op.f("ck_surface_sessions_surface_session_inbound_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["surface_id"],
            ["devices.id"],
            ondelete="CASCADE",
            name=op.f("fk_surface_sessions_surface_id_devices"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
            name=op.f("fk_surface_sessions_session_id_sessions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_surface_sessions")),
    )
    op.create_index(
        "uq_surface_sessions_live_key",
        "surface_sessions",
        ["surface_id", "external_key"],
        unique=True,
        postgresql_where=sa.text("rotated_at IS NULL"),
    )
    op.create_table(
        "surface_inbound_receipts",
        sa.Column("surface_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_update_id", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disposition", sa.String(length=32), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "disposition IN ('submitted','input_delivered','command_handled',"
            "'rejected_unpaired','rejected_locked','rejected_rate',"
            "'rejected_active_run','rejected_admission','ignored_media',"
            "'ignored_chat_kind')",
            name=op.f("ck_surface_inbound_receipts_surface_receipt_disposition_closed"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            ondelete="SET NULL",
            name=op.f("fk_surface_inbound_receipts_run_id_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="SET NULL",
            name=op.f("fk_surface_inbound_receipts_session_id_sessions"),
        ),
        sa.ForeignKeyConstraint(
            ["surface_id"],
            ["devices.id"],
            ondelete="CASCADE",
            name=op.f("fk_surface_inbound_receipts_surface_id_devices"),
        ),
        sa.PrimaryKeyConstraint(
            "surface_id",
            "external_update_id",
            name=op.f("pk_surface_inbound_receipts"),
        ),
    )
    op.create_table(
        "surface_replies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("surface_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chat_ref", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("chunks_total", sa.Integer(), nullable=True),
        sa.Column("chunks_sent", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','dispatched','failed')",
            name=op.f("ck_surface_replies_surface_reply_status_closed"),
        ),
        sa.CheckConstraint(
            "chunks_total IS NULL OR chunks_total >= 0",
            name=op.f("ck_surface_replies_surface_reply_chunks_total_nonnegative"),
        ),
        sa.CheckConstraint(
            "chunks_sent >= 0 AND (chunks_total IS NULL OR chunks_sent <= chunks_total)",
            name=op.f("ck_surface_replies_surface_reply_chunks_sent_bounded"),
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_surface_replies_surface_reply_attempts_nonnegative"),
        ),
        sa.CheckConstraint(
            "(claimed_by IS NULL AND claimed_until IS NULL) OR "
            "(claimed_by IS NOT NULL AND claimed_until IS NOT NULL)",
            name=op.f("ck_surface_replies_surface_reply_claim_consistent"),
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND settled_at IS NULL) OR "
            "(status <> 'pending' AND settled_at IS NOT NULL)",
            name=op.f("ck_surface_replies_surface_reply_settlement_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            ondelete="CASCADE",
            name=op.f("fk_surface_replies_run_id_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["surface_id"],
            ["devices.id"],
            ondelete="CASCADE",
            name=op.f("fk_surface_replies_surface_id_devices"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_surface_replies")),
        sa.UniqueConstraint("run_id", name="uq_surface_replies_run_id"),
    )
    op.create_index(
        "ix_surface_replies_due",
        "surface_replies",
        ["status", "next_attempt_at"],
    )
    for table in (
        "surface_pairing_codes",
        "surface_pairings",
        "surface_sessions",
        "surface_replies",
    ):
        _tenant_policy(table)
    _surface_policy("surface_sender_lockouts")
    _surface_policy("surface_inbound_receipts")


def downgrade() -> None:
    op.drop_index("ix_surface_replies_due", table_name="surface_replies")
    op.drop_table("surface_replies")
    op.drop_table("surface_inbound_receipts")
    op.drop_index("uq_surface_sessions_live_key", table_name="surface_sessions")
    op.drop_table("surface_sessions")
    op.drop_table("surface_sender_lockouts")
    op.drop_index("uq_surface_pairings_live_sender", table_name="surface_pairings")
    op.drop_table("surface_pairings")
    op.drop_table("surface_pairing_codes")
    _replace_check(
        "devices",
        "ck_devices_device_surface_routing",
        _DEVICE_SURFACE_ROUTING_BEFORE,
    )
    _replace_check(
        "devices",
        "ck_devices_device_push_provider_closed",
        _DEVICE_PUSH_PROVIDERS_BEFORE,
    )
