"""Add session-bound browser task grants (ADR-0129).

A task grant is created from a browser.act approval card and holds scope,
pins and counters only: no page URL, profile material, cookie or credential
column. The thirty-minute window, two-hundred-action cap and 4,096 typed
characters are check constraints as well as code constants.

Revision ID: f8db9d463210
Revises: 524f16dfc8f9
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8db9d463210"
down_revision: str | Sequence[str] | None = "524f16dfc8f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_END_REASONS = (
    "'expired','exhausted','revoked','superseded','profile_changed',"
    "'profile_revoked','policy_changed','agent_changed','scope_removed'"
)


def upgrade() -> None:
    op.create_table(
        "browser_task_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("profile_generation", sa.Integer(), nullable=False),
        sa.Column("agent_version", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("path_prefix", sa.Text(), nullable=False),
        sa.Column("max_actions", sa.Integer(), nullable=False),
        sa.Column("actions_used", sa.Integer(), nullable=False),
        sa.Column(
            "typed_characters", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "profile_generation >= 0",
            name=op.f("ck_browser_task_grants_generation_nonnegative"),
        ),
        sa.CheckConstraint(
            "path_prefix ~ '^/[A-Za-z0-9._~-]{1,64}$'",
            name=op.f("ck_browser_task_grants_path_prefix_segment"),
        ),
        sa.CheckConstraint(
            "max_actions BETWEEN 1 AND 200",
            name=op.f("ck_browser_task_grants_max_actions_bounded"),
        ),
        sa.CheckConstraint(
            "actions_used >= 0 AND actions_used <= max_actions",
            name=op.f("ck_browser_task_grants_actions_used_bounded"),
        ),
        sa.CheckConstraint(
            "typed_characters BETWEEN 0 AND 4096",
            name=op.f("ck_browser_task_grants_typed_characters_bounded"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + interval '30 minutes'",
            name=op.f("ck_browser_task_grants_time_window"),
        ),
        sa.CheckConstraint(
            f"end_reason IS NULL OR end_reason IN ({_END_REASONS})",
            name=op.f("ck_browser_task_grants_end_reason_closed"),
        ),
        sa.CheckConstraint(
            "(ended_at IS NULL) = (end_reason IS NULL)",
            name=op.f("ck_browser_task_grants_end_paired"),
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR end_reason = 'revoked'",
            name=op.f("ck_browser_task_grants_revoked_ends_revoked"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name=op.f("fk_browser_task_grants_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["browser_profiles.id"],
            name=op.f("fk_browser_task_grants_profile_id_browser_profiles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_browser_task_grants")),
        sa.UniqueConstraint("approval_id", name=op.f("uq_browser_task_grants_approval_id")),
    )
    op.create_index(
        "uq_browser_task_grants_active_session",
        "browser_task_grants",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.execute(
        "CREATE INDEX ix_browser_task_grants_tenant_principal_created "
        "ON browser_task_grants (tenant_id, principal_id, created_at DESC, id DESC)"
    )
    op.create_index(
        "ix_browser_task_grants_open_expiry",
        "browser_task_grants",
        ["expires_at"],
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_index(
        "ix_browser_task_grants_profile_open",
        "browser_task_grants",
        ["profile_id"],
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.execute("ALTER TABLE browser_task_grants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE browser_task_grants FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY browser_task_grants_tenant_isolation ON browser_task_grants "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def downgrade() -> None:
    op.drop_index("ix_browser_task_grants_profile_open", table_name="browser_task_grants")
    op.drop_index("ix_browser_task_grants_open_expiry", table_name="browser_task_grants")
    op.drop_index(
        "ix_browser_task_grants_tenant_principal_created", table_name="browser_task_grants"
    )
    op.drop_index("uq_browser_task_grants_active_session", table_name="browser_task_grants")
    op.drop_table("browser_task_grants")
