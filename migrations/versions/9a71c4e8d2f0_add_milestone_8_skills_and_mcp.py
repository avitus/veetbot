"""Add Milestone 8 skill and MCP persistence.

Revision ID: 9a71c4e8d2f0
Revises: f2a6d74b9c10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9a71c4e8d2f0"
down_revision: str | Sequence[str] | None = "f2a6d74b9c10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skills")),
        sa.UniqueConstraint("tenant_id", "name", name="uq_skills_tenant_name"),
    )
    op.create_table(
        "skill_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("required_tools", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_tokens", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("package_key", sa.Text(), nullable=False),
        sa.Column("package_bytes", sa.BigInteger(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("trust", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("authored_by_run_id", sa.Uuid(), nullable=True),
        sa.Column("authored_by_principal_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["authored_by_run_id"],
            ["runs.id"],
            name=op.f("fk_skill_revisions_authored_by_run_id_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["skill_id"],
            ["skills.id"],
            name=op.f("fk_skill_revisions_skill_id_skills"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill_revisions")),
        sa.UniqueConstraint("skill_id", "revision", name="uq_skill_revisions_skill_revision"),
    )
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("server_id", sa.Text(), nullable=False),
        sa.Column("transport", sa.Text(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column(
            "operator_configured", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("auth_scheme", sa.Text(), nullable=False),
        sa.Column("auth_name", sa.Text(), nullable=True),
        sa.Column("credential_ref", sa.Text(), nullable=True),
        sa.Column("token_endpoint", sa.Text(), nullable=True),
        sa.Column(
            "token_scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("side_effect", sa.Text(), nullable=False),
        sa.Column("risk", sa.Text(), nullable=False),
        sa.Column("idempotency", sa.Text(), nullable=False),
        sa.Column("required_scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("maximum_output_bytes", sa.BigInteger(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mcp_servers")),
        sa.UniqueConstraint("tenant_id", "server_id", name="uq_mcp_servers_tenant_server"),
    )
    op.create_table(
        "mcp_tool_catalog",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("server_id", sa.Text(), nullable=False),
        sa.Column("catalog_hash", sa.String(length=64), nullable=False),
        sa.Column("remote_name", sa.Text(), nullable=False),
        sa.Column("registry_name", sa.Text(), nullable=False),
        sa.Column("input_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "server_id"],
            ["mcp_servers.tenant_id", "mcp_servers.server_id"],
            name="fk_mcp_tool_catalog_tenant_server_mcp_servers",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mcp_tool_catalog")),
        sa.UniqueConstraint(
            "tenant_id",
            "server_id",
            "catalog_hash",
            "remote_name",
            name="uq_mcp_catalog_generation_tool",
        ),
    )
    for table in ("skills", "mcp_servers", "mcp_tool_catalog"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON {table} "
            "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
            "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
        )
    op.execute("ALTER TABLE skill_revisions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE skill_revisions FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY skill_revisions_tenant_isolation ON skill_revisions "
        "USING (EXISTS (SELECT 1 FROM skills WHERE skills.id = skill_revisions.skill_id)) "
        "WITH CHECK (EXISTS (SELECT 1 FROM skills WHERE skills.id = skill_revisions.skill_id))"
    )


def downgrade() -> None:
    op.drop_table("mcp_tool_catalog")
    op.drop_table("mcp_servers")
    op.drop_table("skill_revisions")
    op.drop_table("skills")
