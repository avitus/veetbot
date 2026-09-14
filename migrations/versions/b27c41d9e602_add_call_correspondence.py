"""Add principal-scoped call correspondence records.

Revision ID: b27c41d9e602
Revises: a8e6c3f9d127
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b27c41d9e602"
down_revision: str | Sequence[str] | None = "a8e6c3f9d127"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("notification_kind_closed", "notification_outbox", type_="check")
    op.create_check_constraint(
        "notification_kind_closed",
        "notification_outbox",
        "kind IN ('approval_requested','question_asked','run_failed','schedule_run_finished',"
        "'schedule_occurrence_skipped','ops_alert','ops_recovered','test','device_invocation','call_finished')",
    )
    op.drop_constraint("device_muted_kinds_closed", "devices", type_="check")
    op.create_check_constraint(
        "device_muted_kinds_closed",
        "devices",
        "jsonb_typeof(muted_kinds) = 'array' AND muted_kinds <@ "
        '\'["approval_requested","question_asked","run_failed","schedule_run_finished",'
        '"schedule_occurrence_skipped","ops_alert","ops_recovered","test","device_invocation","call_finished"]\'::jsonb',
    )
    op.create_table(
        "call_records",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(collation="C"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "principal_id", "kind", "key"),
        sa.CheckConstraint("revision > 0", name="call_record_revision_positive"),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="call_record_payload_object"),
    )
    op.create_index(
        "ix_call_records_owner_updated",
        "call_records",
        ["tenant_id", "principal_id", "updated_at"],
    )
    op.execute("ALTER TABLE call_records ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE call_records FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY call_records_tenant_isolation ON call_records "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def downgrade() -> None:
    op.execute("DELETE FROM notification_outbox WHERE kind = 'call_finished'")
    op.execute("UPDATE devices SET muted_kinds = muted_kinds - 'call_finished'")
    op.drop_constraint("notification_kind_closed", "notification_outbox", type_="check")
    op.create_check_constraint(
        "notification_kind_closed",
        "notification_outbox",
        "kind IN ('approval_requested','question_asked','run_failed','schedule_run_finished',"
        "'schedule_occurrence_skipped','ops_alert','ops_recovered','test','device_invocation')",
    )
    op.drop_constraint("device_muted_kinds_closed", "devices", type_="check")
    op.create_check_constraint(
        "device_muted_kinds_closed",
        "devices",
        "jsonb_typeof(muted_kinds) = 'array' AND muted_kinds <@ "
        '\'["approval_requested","question_asked","run_failed","schedule_run_finished",'
        '"schedule_occurrence_skipped","ops_alert","ops_recovered","test","device_invocation"]\'::jsonb',
    )
    op.drop_index("ix_call_records_owner_updated", table_name="call_records")
    op.drop_table("call_records")
