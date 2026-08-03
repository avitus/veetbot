"""Create the durable runtime schema.

Revision ID: 50f3618a97d0
Revises: a3f19c2b7d04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "50f3618a97d0"
down_revision: str | Sequence[str] | None = "a3f19c2b7d04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create all Milestone 2 persistence structures."""
    op.create_table(
        "agents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("model_policy", sa.Text(), nullable=False),
        sa.Column("enabled_tools", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "enabled_skills",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("policy_profile", sa.Text(), nullable=False),
        sa.Column("limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", "version", name=op.f("pk_agents")),
    )
    op.create_table(
        "export_consent",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "principal_id", name=op.f("pk_export_consent")),
    )
    op.create_table(
        "model_prices",
        sa.Column("price_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_per_mtok", sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column("cached_input_per_mtok", sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column("cache_write_per_mtok", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("output_per_mtok", sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column("reasoning_per_mtok", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("reasoning_priced_separately", sa.Boolean(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("price_id", name=op.f("pk_model_prices")),
        sa.UniqueConstraint("provider", "model", "effective_at", name="uq_model_prices_effective"),
    )
    op.create_table(
        "projection_watermarks",
        sa.Column("projection_name", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("watermark_seq", sa.BigInteger(), nullable=False),
        sa.Column("builder_version", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("projection_name", "scope", name=op.f("pk_projection_watermarks")),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("agent_version", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "next_event_sequence", sa.BigInteger(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
    )
    op.create_index(
        "ix_sessions_agent_created", "sessions", ["agent_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_sessions_tenant_principal_updated",
        "sessions",
        ["tenant_id", "principal_id", "updated_at"],
        unique=False,
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("parent_run_id", sa.UUID(), nullable=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("agent_version", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("step_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("model_call_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("tool_call_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_epoch", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("priority", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("final_message", sa.Text(), nullable=True),
        sa.Column("export_consent", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("seed_event_sequence", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["parent_run_id"],
            ["runs.id"],
            name=op.f("fk_runs_parent_run_id_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name=op.f("fk_runs_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
    )
    op.create_index(
        "ix_runs_active_deadline",
        "runs",
        ["deadline_at"],
        unique=False,
        postgresql_where=sa.text(
            "deadline_at IS NOT NULL AND status IN "
            "('RUNNING','WAITING_FOR_APPROVAL','WAITING_FOR_USER')"
        ),
    )
    op.create_index("ix_runs_lease_expires", "runs", ["lease_expires_at"], unique=False)
    op.create_index(
        "ix_runs_queue_claim",
        "runs",
        ["status", "priority", "created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'QUEUED'"),
    )
    op.create_index("ix_runs_session_created", "runs", ["session_id", "created_at"], unique=False)
    op.create_index("ix_runs_status_created", "runs", ["status", "created_at"], unique=False)
    op.create_index(
        "uq_runs_one_active_per_session",
        "runs",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('COMPLETED','FAILED','CANCELLED')"),
    )
    op.create_table(
        "session_history_items",
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("item_index", sa.SmallInteger(), nullable=False),
        sa.Column("item", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("builder_version", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name=op.f("fk_session_history_items_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "session_id", "sequence", "item_index", name=op.f("pk_session_history_items")
        ),
    )
    op.create_table(
        "artifacts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("storage_uri", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_artifacts_run_id_runs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name=op.f("fk_artifacts_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifacts")),
    )
    op.create_table(
        "checkpoints",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("last_event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("full", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("base_version", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_checkpoints_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_checkpoints")),
        sa.UniqueConstraint("run_id", "version", name="uq_checkpoints_run_version"),
    )
    op.create_index(
        "ix_checkpoints_run_created", "checkpoints", ["run_id", "created_at"], unique=False
    )
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload_schema_version", sa.SmallInteger(), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column("derivation_key", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_events_run_id_runs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name=op.f("fk_events_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        sa.UniqueConstraint("session_id", "sequence", name="uq_events_session_sequence"),
    )
    op.create_index(
        "ix_events_event_type_created", "events", ["event_type", "created_at"], unique=False
    )
    op.create_index("ix_events_run_id", "events", ["run_id", "id"], unique=False)
    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_idempotency_keys_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_idempotency_keys")),
    )
    op.create_table(
        "model_calls",
        sa.Column("attempt_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("step_number", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("model_policy", sa.Text(), nullable=False),
        sa.Column("registry_version", sa.Text(), nullable=False),
        sa.Column("prefix_sha256", sa.String(length=64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
        sa.Column("cost", sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column("cost_source", sa.Text(), nullable=False),
        sa.Column("price_id", sa.Text(), nullable=True),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        sa.Column("error_kind", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["price_id"],
            ["model_prices.price_id"],
            name=op.f("fk_model_calls_price_id_model_prices"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_model_calls_run_id_runs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name=op.f("fk_model_calls_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("attempt_id", name=op.f("pk_model_calls")),
    )
    op.create_index(
        "ix_model_calls_run_step_attempt",
        "model_calls",
        ["run_id", "step_number", "attempt_number"],
        unique=False,
    )
    op.create_index("ix_model_calls_session", "model_calls", ["session_id"], unique=False)
    op.create_index(
        "ix_model_calls_tenant_started", "model_calls", ["tenant_id", "started_at"], unique=False
    )
    op.create_table(
        "tool_invocations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("step_number", sa.Integer(), nullable=False),
        sa.Column("provider_call_id", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("tool_version", sa.Text(), nullable=False),
        sa.Column("tool_source", sa.Text(), server_default="builtin", nullable=False),
        sa.Column("server_id", sa.Text(), nullable=True),
        sa.Column("idempotency_class", sa.Text(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), server_default="1", nullable=False),
        sa.Column("raw_arguments", sa.Text(), nullable=False),
        sa.Column("arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("normalized_arguments_hash", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("effect_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result_item", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("suspended_kind", sa.Text(), nullable=True),
        sa.Column("suspended_ref", sa.Text(), nullable=True),
        sa.Column("output_bytes", sa.BigInteger(), nullable=True),
        sa.Column("truncated", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("artifact_id", sa.UUID(), nullable=True),
        sa.Column("outcome_status", sa.Text(), nullable=True),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("origin_trust", sa.Text(), nullable=False),
        sa.Column("parallel_group", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_tool_invocations_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_invocations")),
        sa.UniqueConstraint("idempotency_key", name="uq_tool_invocations_idempotency_key"),
    )
    op.create_index(
        "ix_tool_invocations_run_step", "tool_invocations", ["run_id", "step_number"], unique=False
    )
    op.create_table(
        "trajectory_projection",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("first_sequence", sa.BigInteger(), nullable=False),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False),
        sa.Column("terminal", sa.Boolean(), nullable=False),
        sa.Column("builder_version", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_trajectory_projection_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_trajectory_projection")),
    )
    op.create_table(
        "approvals",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("tool_invocation_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("resolution", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_approvals_run_id_runs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["tool_invocation_id"],
            ["tool_invocations.id"],
            name=op.f("fk_approvals_tool_invocation_id_tool_invocations"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
    )
    op.create_table(
        "derived_event_keys",
        sa.Column("derivation_key", sa.Text(), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_derived_event_keys_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("derivation_key", name=op.f("pk_derived_event_keys")),
    )
    op.create_table(
        "trajectory_exports",
        sa.Column("export_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("artifact_id", sa.UUID(), nullable=False),
        sa.Column("builder_version", sa.Text(), nullable=False),
        sa.Column("ruleset_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifacts.id"],
            name=op.f("fk_trajectory_exports_artifact_id_artifacts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_trajectory_exports_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("export_id", name=op.f("pk_trajectory_exports")),
        sa.UniqueConstraint("run_id", name="uq_trajectory_exports_run"),
    )
    op.create_index(
        "ix_trajectory_exports_tenant_principal",
        "trajectory_exports",
        ["tenant_id", "principal_id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove all Milestone 2 persistence structures."""
    op.drop_index("ix_trajectory_exports_tenant_principal", table_name="trajectory_exports")
    op.drop_table("trajectory_exports")
    op.drop_table("derived_event_keys")
    op.drop_table("approvals")
    op.drop_table("trajectory_projection")
    op.drop_index("ix_tool_invocations_run_step", table_name="tool_invocations")
    op.drop_table("tool_invocations")
    op.drop_index("ix_model_calls_tenant_started", table_name="model_calls")
    op.drop_index("ix_model_calls_session", table_name="model_calls")
    op.drop_index("ix_model_calls_run_step_attempt", table_name="model_calls")
    op.drop_table("model_calls")
    op.drop_table("idempotency_keys")
    op.drop_index("ix_events_run_id", table_name="events")
    op.drop_index("ix_events_event_type_created", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_checkpoints_run_created", table_name="checkpoints")
    op.drop_table("checkpoints")
    op.drop_table("artifacts")
    op.drop_table("session_history_items")
    op.drop_index(
        "uq_runs_one_active_per_session",
        table_name="runs",
        postgresql_where=sa.text("status NOT IN ('COMPLETED','FAILED','CANCELLED')"),
    )
    op.drop_index("ix_runs_status_created", table_name="runs")
    op.drop_index("ix_runs_session_created", table_name="runs")
    op.drop_index(
        "ix_runs_queue_claim", table_name="runs", postgresql_where=sa.text("status = 'QUEUED'")
    )
    op.drop_index("ix_runs_lease_expires", table_name="runs")
    op.drop_index(
        "ix_runs_active_deadline",
        table_name="runs",
        postgresql_where=sa.text(
            "deadline_at IS NOT NULL AND status IN "
            "('RUNNING','WAITING_FOR_APPROVAL','WAITING_FOR_USER')"
        ),
    )
    op.drop_table("runs")
    op.drop_index("ix_sessions_tenant_principal_updated", table_name="sessions")
    op.drop_index("ix_sessions_agent_created", table_name="sessions")
    op.drop_table("sessions")
    op.drop_table("projection_watermarks")
    op.drop_table("model_prices")
    op.drop_table("export_consent")
    op.drop_table("agents")
