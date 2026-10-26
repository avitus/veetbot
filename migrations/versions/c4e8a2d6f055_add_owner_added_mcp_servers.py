"""Owner-added MCP servers and their sealed sign-ins (ADR-0255).

Revision ID: c4e8a2d6f055
Revises: b7d2e4f6a1c3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4e8a2d6f055"
down_revision: str | Sequence[str] | None = "b7d2e4f6a1c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "mcp_servers",
        sa.Column("owner_managed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_check_constraint(
        "ck_mcp_servers_owner_managed_http",
        "mcp_servers",
        "NOT owner_managed OR (transport = 'http' AND NOT operator_configured)",
    )
    op.create_table(
        "mcp_server_authorizations",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("server_id", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=True),
        sa.Column("client_id", sa.Text(), nullable=True),
        sa.Column("client_secret_sealed", sa.Text(), nullable=True),
        sa.Column("client_auth_method", sa.Text(), nullable=True),
        sa.Column("access_token_sealed", sa.Text(), nullable=True),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_token_sealed", sa.Text(), nullable=True),
        sa.Column("signed_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signed_in_by", sa.Text(), nullable=True),
        sa.Column("pending_state_hash", sa.String(64), nullable=True),
        sa.Column("pending_verifier_sealed", sa.Text(), nullable=True),
        sa.Column("pending_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pending_principal_id", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "server_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "server_id"],
            ["mcp_servers.tenant_id", "mcp_servers.server_id"],
            name="fk_mcp_server_authorizations_server",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "client_auth_method IS NULL OR client_auth_method IN "
            "('none', 'client_secret_post', 'client_secret_basic')",
            name="ck_mcp_server_authorizations_auth_method",
        ),
        sa.CheckConstraint(
            "(pending_state_hash IS NULL) = (pending_verifier_sealed IS NULL) "
            "AND (pending_state_hash IS NULL) = (pending_expires_at IS NULL)",
            name="ck_mcp_server_authorizations_pending_complete",
        ),
    )
    op.create_index(
        "uq_mcp_server_authorizations_pending_state",
        "mcp_server_authorizations",
        ["tenant_id", "pending_state_hash"],
        unique=True,
        postgresql_where=sa.text("pending_state_hash IS NOT NULL"),
    )
    op.execute("ALTER TABLE mcp_server_authorizations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE mcp_server_authorizations FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY mcp_server_authorizations_tenant_isolation "
        "ON mcp_server_authorizations "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM mcp_servers WHERE owner_managed)")
    ):
        raise RuntimeError(
            "Remove owner-added MCP servers in Settings before downgrade; "
            "their configuration must not be lost"
        )
    op.drop_table("mcp_server_authorizations")
    op.drop_constraint("ck_mcp_servers_owner_managed_http", "mcp_servers", type_="check")
    op.drop_column("mcp_servers", "owner_managed")
