"""Add Milestone 12 — Notifications and device identity persistence.

Revision ID: c7e9a4f2d105
Revises: b6f4c2d8e901
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7e9a4f2d105"
down_revision: str | Sequence[str] | None = "b6f4c2d8e901"
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


def _delivery_policy() -> None:
    predicate = (
        "EXISTS (SELECT 1 FROM notification_outbox n "
        "JOIN devices d ON d.id = notification_deliveries.device_id "
        "WHERE n.id = notification_deliveries.notification_id "
        "AND n.tenant_id = current_setting('agent_core.tenant_id', true) "
        "AND d.tenant_id = n.tenant_id AND d.principal_id = n.principal_id)"
    )
    op.execute("ALTER TABLE notification_deliveries ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE notification_deliveries FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY notification_deliveries_tenant_isolation "
        f"ON notification_deliveries USING ({predicate}) WITH CHECK ({predicate})"
    )


def upgrade() -> None:
    op.create_table(
        "devices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("client_device_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("platform", sa.String(length=64), nullable=False),
        sa.Column("app_bundle_id", sa.Text(), nullable=True),
        sa.Column("push_provider", sa.String(length=32), nullable=True),
        sa.Column("push_token", sa.Text(), nullable=True),
        sa.Column("push_environment", sa.String(length=32), nullable=True),
        sa.Column("push_token_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("push_token_invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "muted_kinds",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('mobile','laptop','desktop','web','cli','surface')",
            name=op.f("ck_devices_device_kind_closed"),
        ),
        sa.CheckConstraint(
            "push_provider IS NULL OR push_provider IN ('apns','telegram')",
            name=op.f("ck_devices_device_push_provider_closed"),
        ),
        sa.CheckConstraint(
            "push_environment IS NULL OR push_environment IN ('sandbox','production')",
            name=op.f("ck_devices_device_push_environment_closed"),
        ),
        sa.CheckConstraint(
            "(push_provider IS NULL AND push_token IS NULL) OR "
            "(push_provider IS NOT NULL AND push_token IS NOT NULL)",
            name=op.f("ck_devices_device_push_routing_pair"),
        ),
        sa.CheckConstraint(
            "(push_provider = 'apns' AND push_environment IS NOT NULL) OR "
            "((push_provider IS NULL OR push_provider <> 'apns') "
            "AND push_environment IS NULL)",
            name=op.f("ck_devices_device_push_environment_provider"),
        ),
        sa.CheckConstraint(
            "(kind = 'surface' AND "
            "(push_provider IS NULL OR push_provider = 'telegram')) OR "
            "(kind <> 'surface' AND (push_provider IS NULL OR push_provider <> 'telegram'))",
            name=op.f("ck_devices_device_surface_routing"),
        ),
        sa.CheckConstraint(
            "status IN ('active','revoked')",
            name=op.f("ck_devices_device_status_closed"),
        ),
        sa.CheckConstraint(
            "(status = 'revoked' AND revoked_at IS NOT NULL AND push_token IS NULL) OR "
            "(status = 'active' AND revoked_at IS NULL)",
            name=op.f("ck_devices_device_revocation_consistent"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(muted_kinds) = 'array' AND muted_kinds <@ "
            '\'["approval_requested","question_asked","run_failed",'
            '"schedule_run_finished","schedule_occurrence_skipped",'
            '"ops_alert","ops_recovered","test"]\'::jsonb',
            name=op.f("ck_devices_device_muted_kinds_closed"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_devices")),
        sa.UniqueConstraint(
            "tenant_id",
            "principal_id",
            "client_device_id",
            name="uq_devices_principal_client_identity",
        ),
    )
    op.create_index(
        "ix_devices_tenant_principal_created",
        "devices",
        ["tenant_id", "principal_id", "created_at", "id"],
    )
    op.create_index(
        "uq_devices_active_push_token",
        "devices",
        ["push_provider", "push_token"],
        unique=True,
        postgresql_where=sa.text("push_token IS NOT NULL AND status = 'active'"),
    )
    op.create_table(
        "device_registration_idempotency_keys",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "response",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "principal_id",
            "key",
            name=op.f("pk_device_registration_idempotency_keys"),
        ),
    )
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("question_id", sa.Uuid(), nullable=True),
        sa.Column("schedule_id", sa.Uuid(), nullable=True),
        sa.Column("occurrence_id", sa.Uuid(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("priority", sa.SmallInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('approval_requested','question_asked','run_failed',"
            "'schedule_run_finished','schedule_occurrence_skipped','ops_alert',"
            "'ops_recovered','test')",
            name=op.f("ck_notification_outbox_notification_kind_closed"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','dispatched','superseded','expired','failed')",
            name=op.f("ck_notification_outbox_notification_status_closed"),
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_notification_outbox_notification_attempts_nonnegative"),
        ),
        sa.CheckConstraint(
            "priority >= 0",
            name=op.f("ck_notification_outbox_notification_priority_nonnegative"),
        ),
        sa.CheckConstraint(
            "(claimed_by IS NULL AND claimed_until IS NULL) OR "
            "(claimed_by IS NOT NULL AND claimed_until IS NOT NULL)",
            name=op.f("ck_notification_outbox_notification_claim_pair"),
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND settled_at IS NULL) OR "
            "(status <> 'pending' AND settled_at IS NOT NULL)",
            name=op.f("ck_notification_outbox_notification_settlement_consistent"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_outbox")),
        sa.UniqueConstraint("dedupe_key", name="uq_notification_outbox_dedupe_key"),
    )
    op.create_index(
        "ix_notification_outbox_due",
        "notification_outbox",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_notification_outbox_tenant_principal_created",
        "notification_outbox",
        ["tenant_id", "principal_id", "created_at", "id"],
    )
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("notification_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("provider_reason", sa.Text(), nullable=True),
        sa.Column("provider_id", sa.Text(), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt > 0",
            name=op.f("ck_notification_deliveries_notification_delivery_attempt_positive"),
        ),
        sa.CheckConstraint(
            "outcome IN ('delivered','retry','unregistered','rejected','skipped')",
            name=op.f("ck_notification_deliveries_notification_delivery_outcome_closed"),
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            ondelete="RESTRICT",
            name=op.f("fk_notification_deliveries_device_id_devices"),
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notification_outbox.id"],
            ondelete="RESTRICT",
            name=op.f("fk_notification_deliveries_notification_id_notification_outbox"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_deliveries")),
        sa.UniqueConstraint(
            "notification_id",
            "device_id",
            "attempt",
            name="uq_notification_delivery_attempt",
        ),
    )
    _tenant_policy("devices")
    _tenant_policy("device_registration_idempotency_keys")
    _tenant_policy("notification_outbox")
    _delivery_policy()


def downgrade() -> None:
    op.drop_table("notification_deliveries")
    op.drop_table("notification_outbox")
    op.drop_table("device_registration_idempotency_keys")
    op.drop_table("devices")
