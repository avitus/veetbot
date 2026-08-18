"""Declarative PostgreSQL rows confined to the persistence adapter."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    Sequence,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

MEMORY_POSITION_SEQUENCE = Sequence("memory_store_position_seq")

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Shared metadata for Alembic autogenerate and row mappings."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class AgentRow(Base):
    __tablename__ = "agents"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    version: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    instructions: Mapped[str] = mapped_column(Text)
    model_policy: Mapped[str] = mapped_column(Text)
    enabled_tools: Mapped[list[str]] = mapped_column(JSONB)
    enabled_skills: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    policy_profile: Mapped[str] = mapped_column(Text)
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionRow(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_tenant_principal_updated", "tenant_id", "principal_id", "updated_at"),
        Index("ix_sessions_agent_created", "agent_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text)
    principal_id: Mapped[str] = mapped_column(Text)
    agent_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    agent_version: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    title: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )
    next_event_sequence: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionDeletionRow(Base):
    __tablename__ = "session_deletions"
    __table_args__ = (
        Index("ix_session_deletions_tenant_principal", "tenant_id", "principal_id", "deleted_at"),
    )

    session_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text)
    principal_id: Mapped[str] = mapped_column(Text)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionDeletionArtifactRow(Base):
    __tablename__ = "session_deletion_artifacts"

    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("session_deletions.session_id", ondelete="CASCADE"),
        primary_key=True,
    )
    artifact_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text)
    artifact: Mapped[dict[str, Any]] = mapped_column(JSONB)


class RunRow(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_status_created", "status", "created_at"),
        Index("ix_runs_lease_expires", "lease_expires_at"),
        Index("ix_runs_session_created", "session_id", "created_at"),
        Index(
            "ix_runs_queue_claim",
            "status",
            "priority",
            "created_at",
            postgresql_where=text("status = 'QUEUED'"),
        ),
        Index(
            "uq_runs_one_active_per_session",
            "session_id",
            unique=True,
            postgresql_where=text("status NOT IN ('COMPLETED','FAILED','CANCELLED')"),
        ),
        Index(
            "ix_runs_active_deadline",
            "deadline_at",
            postgresql_where=text(
                "deadline_at IS NOT NULL AND status IN "
                "('RUNNING','WAITING_FOR_APPROVAL','WAITING_FOR_USER')"
            ),
        ),
        Index(
            "uq_runs_parent_skill_review",
            "parent_run_id",
            unique=True,
            postgresql_where=text("parent_run_id IS NOT NULL AND run_kind = 'skill_review'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE")
    )
    parent_run_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL")
    )
    run_kind: Mapped[str] = mapped_column(Text, server_default=text("'interactive'"))
    tenant_id: Mapped[str] = mapped_column(Text)
    principal_scopes: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    agent_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    agent_version: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    step_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    model_call_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    tool_call_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB)
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_epoch: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    priority: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    final_message: Mapped[str | None] = mapped_column(Text)
    export_consent: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    provider_pin: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    seed_event_sequence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_events_session_sequence"),
        Index("ix_events_run_id", "run_id", "id"),
        Index("ix_events_event_type_created", "event_type", "created_at"),
        Index("ix_events_type_session_sequence", "event_type", "session_id", "sequence"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE")
    )
    run_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE")
    )
    sequence: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(Text)
    payload_schema_version: Mapped[int] = mapped_column(SmallInteger)
    actor_type: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    trace_id: Mapped[str | None] = mapped_column(Text)
    derivation_key: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CheckpointRow(Base):
    __tablename__ = "checkpoints"
    __table_args__ = (
        UniqueConstraint("run_id", "version", name="uq_checkpoints_run_version"),
        Index("ix_checkpoints_run_created", "run_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE")
    )
    version: Mapped[int] = mapped_column(Integer)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB)
    last_event_sequence: Mapped[int] = mapped_column(BigInteger)
    full: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    base_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ToolInvocationRow(Base):
    __tablename__ = "tool_invocations"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_tool_invocations_idempotency_key"),
        Index("ix_tool_invocations_run_step", "run_id", "step_number"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE")
    )
    session_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    step_number: Mapped[int] = mapped_column(Integer)
    provider_call_id: Mapped[str] = mapped_column(Text)
    tool_name: Mapped[str] = mapped_column(Text)
    tool_version: Mapped[str] = mapped_column(Text)
    tool_source: Mapped[str] = mapped_column(Text, server_default=text("'builtin'"))
    server_id: Mapped[str | None] = mapped_column(Text)
    idempotency_class: Mapped[str] = mapped_column(Text)
    side_effect: Mapped[str] = mapped_column(Text, server_default=text("'none'"))
    risk: Mapped[str] = mapped_column(Text, server_default=text("'low'"))
    attempt_number: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    raw_arguments: Mapped[str] = mapped_column(Text)
    arguments: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    normalized_arguments_hash: Mapped[str | None] = mapped_column(Text)
    effective_arguments_hash: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    effect_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result_item: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    structured_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    policy_decision: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    suspended_kind: Mapped[str | None] = mapped_column(Text)
    suspended_ref: Mapped[str | None] = mapped_column(Text)
    output_bytes: Mapped[int | None] = mapped_column(BigInteger)
    truncated: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    artifact_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    outcome_status: Mapped[str | None] = mapped_column(Text)
    reason_code: Mapped[str | None] = mapped_column(Text)
    origin_trust: Mapped[str] = mapped_column(Text)
    parallel_group: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ApprovalRow(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        Index(
            "ix_approvals_tenant_status_created",
            "tenant_id",
            "status",
            "created_at",
            "id",
        ),
        Index("ix_approvals_run_id", "run_id"),
        Index(
            "ix_approvals_pending_expiry",
            "status",
            "expires_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index("uq_approvals_action_id", "action_id", unique=True),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text)
    principal_id: Mapped[str] = mapped_column(Text)
    session_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE")
    )
    action_kind: Mapped[str] = mapped_column(Text)
    action_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    risk: Mapped[str] = mapped_column(Text)
    policy_version: Mapped[str] = mapped_column(Text)
    revalidated_policy_version: Mapped[str | None] = mapped_column(Text)
    tool_invocation_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tool_invocations.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(32))
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)
    resolution: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(Text)


class PolicyProfileRow(Base):
    __tablename__ = "policy_profiles"

    policy_version: Mapped[str] = mapped_column(Text, primary_key=True)
    profile_name: Mapped[str] = mapped_column(Text)
    profile_sha256: Mapped[str] = mapped_column(Text)
    hardline_sha256: Mapped[str] = mapped_column(Text)
    rule_count: Mapped[int] = mapped_column(Integer)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    loaded_by: Mapped[str] = mapped_column(Text)


class ProcessEventRow(Base):
    __tablename__ = "process_events"
    __table_args__ = (Index("ix_process_events_type_created", "event_type", "created_at"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    event_type: Mapped[str] = mapped_column(Text)
    payload_schema_version: Mapped[int] = mapped_column(Integer)
    actor_type: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    derivation_key: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvalScenarioRunRow(Base):
    __tablename__ = "eval_scenario_runs"
    __table_args__ = (
        UniqueConstraint(
            "scenario_id",
            "build_ref",
            "judge_version",
            "repeat_index",
            name="uq_eval_scenario_run_build_repeat",
        ),
        Index("ix_eval_scenario_runs_suite_build", "suite", "build_ref", "judge_version"),
        Index("ix_eval_scenario_runs_started", "started_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    scenario_id: Mapped[str] = mapped_column(Text)
    suite: Mapped[str] = mapped_column(Text)
    repeat_index: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    judge_version: Mapped[str] = mapped_column(Text)
    build_ref: Mapped[str] = mapped_column(Text)
    score: Mapped[Decimal | None] = mapped_column(Numeric)
    ceiling_hit: Mapped[str | None] = mapped_column(Text)
    policy_failures: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvalScenarioAttemptCostRow(Base):
    __tablename__ = "eval_scenario_attempt_costs"
    __table_args__ = (
        Index("ix_eval_scenario_attempt_costs_started", "started_at"),
        Index("ix_eval_scenario_attempt_costs_scenario_run", "scenario_run_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    scenario_run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("eval_scenario_runs.id", ondelete="CASCADE"),
    )
    cost_usd: Mapped[Decimal] = mapped_column(Numeric)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvalCriterionScoreRow(Base):
    __tablename__ = "eval_criterion_scores"
    __table_args__ = (
        UniqueConstraint("scenario_run_id", "criterion", name="uq_eval_criterion_scenario_run"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    scenario_run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("eval_scenario_runs.id", ondelete="CASCADE"),
    )
    criterion: Mapped[str] = mapped_column(Text)
    observation: Mapped[str] = mapped_column(Text)
    value: Mapped[Decimal] = mapped_column(Numeric)


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        Index("ix_artifacts_expires_at", "expires_at"),
        Index(
            "ix_artifacts_general_expires_at",
            "expires_at",
            postgresql_where=text("origin <> 'trajectory_export' AND expires_at IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text)
    principal_id: Mapped[str] = mapped_column(Text)
    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE")
    )
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(Text)
    storage_uri: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    origin: Mapped[str] = mapped_column(Text, server_default=text("'trajectory_export'"))
    trust: Mapped[str] = mapped_column(Text, server_default=text("'external_untrusted'"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IdempotencyKeyRow(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (Index("ix_idempotency_keys_expires_at", "expires_at"),)

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    principal_id: Mapped[str] = mapped_column(Text, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProjectionWatermarkRow(Base):
    __tablename__ = "projection_watermarks"

    projection_name: Mapped[str] = mapped_column(Text, primary_key=True)
    scope: Mapped[str] = mapped_column(Text, primary_key=True, server_default=text("''"))
    watermark_seq: Mapped[int] = mapped_column(BigInteger)
    builder_version: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DerivedEventKeyRow(Base):
    __tablename__ = "derived_event_keys"

    derivation_key: Mapped[str] = mapped_column(Text, primary_key=True)
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("events.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionHistoryItemRow(Base):
    __tablename__ = "session_history_items"

    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_index: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    item: Mapped[dict[str, Any]] = mapped_column(JSONB)
    builder_version: Mapped[str] = mapped_column(Text)


class TrajectoryProjectionRow(Base):
    __tablename__ = "trajectory_projection"

    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    first_sequence: Mapped[int] = mapped_column(BigInteger)
    last_sequence: Mapped[int] = mapped_column(BigInteger)
    terminal: Mapped[bool] = mapped_column(Boolean)
    builder_version: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExportConsentRow(Base):
    __tablename__ = "export_consent"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    principal_id: Mapped[str] = mapped_column(Text, primary_key=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TrajectoryExportRow(Base):
    __tablename__ = "trajectory_exports"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_trajectory_exports_run"),
        Index("ix_trajectory_exports_tenant_principal", "tenant_id", "principal_id"),
    )

    export_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text)
    principal_id: Mapped[str] = mapped_column(Text)
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE")
    )
    artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("artifacts.id", ondelete="CASCADE")
    )
    builder_version: Mapped[str] = mapped_column(Text)
    ruleset_version: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ModelPriceRow(Base):
    __tablename__ = "model_prices"
    __table_args__ = (
        UniqueConstraint("provider", "model", "effective_at", name="uq_model_prices_effective"),
    )

    price_id: Mapped[str] = mapped_column(Text, primary_key=True)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    input_per_mtok: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    cached_input_per_mtok: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    cache_write_per_mtok: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    output_per_mtok: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    reasoning_per_mtok: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    reasoning_priced_separately: Mapped[bool] = mapped_column(Boolean)
    source: Mapped[str] = mapped_column(Text)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ModelCallRow(Base):
    __tablename__ = "model_calls"
    __table_args__ = (
        Index("ix_model_calls_run_step_attempt", "run_id", "step_number", "attempt_number"),
        Index("ix_model_calls_tenant_started", "tenant_id", "started_at"),
        Index("ix_model_calls_session", "session_id"),
        Index("ix_model_calls_tenant_response", "tenant_id", "response_id"),
    )

    attempt_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE")
    )
    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE")
    )
    tenant_id: Mapped[str] = mapped_column(Text)
    step_number: Mapped[int] = mapped_column(Integer)
    attempt_number: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    model_policy: Mapped[str] = mapped_column(Text)
    registry_version: Mapped[str] = mapped_column(Text)
    prefix_sha256: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer)
    cached_input_tokens: Mapped[int] = mapped_column(Integer)
    cache_write_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer)
    cost: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    cost_source: Mapped[str] = mapped_column(Text)
    price_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("model_prices.price_id", ondelete="SET NULL")
    )
    stop_reason: Mapped[str | None] = mapped_column(Text)
    error_kind: Mapped[str | None] = mapped_column(Text)
    provider_api: Mapped[str] = mapped_column(Text, server_default=text("'chat_completions'"))
    response_id: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    resolved_model: Mapped[str | None] = mapped_column(Text)
    cache_breakpoints_sent: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    cache_breakpoints_dropped: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SkillRow(Base):
    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_skills_tenant_name"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SkillRevisionRow(Base):
    __tablename__ = "skill_revisions"
    __table_args__ = (
        UniqueConstraint("skill_id", "revision", name="uq_skill_revisions_skill_revision"),
        Index(
            "uq_skill_revisions_authoring_invocation",
            "authored_by_invocation_id",
            unique=True,
            postgresql_where=text("authored_by_invocation_id IS NOT NULL"),
        ),
        Index(
            "uq_skill_revisions_archive_invocation",
            "archived_by_invocation_id",
            unique=True,
            postgresql_where=text("archived_by_invocation_id IS NOT NULL"),
        ),
        Index(
            "ix_skill_revisions_skill_status_revision_desc",
            "skill_id",
            "status",
            text("revision DESC"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    skill_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE")
    )
    revision: Mapped[int] = mapped_column(Integer)
    version: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    required_tools: Mapped[list[str]] = mapped_column(JSONB)
    body: Mapped[str] = mapped_column(Text)
    body_tokens: Mapped[int] = mapped_column(Integer)
    content_sha256: Mapped[str] = mapped_column(String(64))
    package_key: Mapped[str] = mapped_column(Text)
    package_bytes: Mapped[int] = mapped_column(BigInteger)
    file_count: Mapped[int] = mapped_column(Integer)
    trust: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    authored_by_run_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="RESTRICT")
    )
    authored_by_principal_id: Mapped[str | None] = mapped_column(Text)
    authored_by_invocation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    authoring_idempotency_key: Mapped[str | None] = mapped_column(Text)
    archived_by_invocation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    archive_idempotency_key: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MCPServerRow(Base):
    __tablename__ = "mcp_servers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "server_id", name="uq_mcp_servers_tenant_server"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text)
    server_id: Mapped[str] = mapped_column(Text)
    transport: Mapped[str] = mapped_column(Text)
    endpoint: Mapped[str] = mapped_column(Text)
    operator_configured: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    auth_scheme: Mapped[str] = mapped_column(Text)
    auth_name: Mapped[str | None] = mapped_column(Text)
    credential_ref: Mapped[str | None] = mapped_column(Text)
    token_endpoint: Mapped[str | None] = mapped_column(Text)
    token_scopes: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    side_effect: Mapped[str] = mapped_column(Text)
    risk: Mapped[str] = mapped_column(Text)
    idempotency: Mapped[str] = mapped_column(Text)
    required_scopes: Mapped[list[str]] = mapped_column(JSONB)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    maximum_output_bytes: Mapped[int] = mapped_column(BigInteger)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MCPToolCatalogRow(Base):
    __tablename__ = "mcp_tool_catalog"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "server_id"],
            ["mcp_servers.tenant_id", "mcp_servers.server_id"],
            name="fk_mcp_tool_catalog_tenant_server_mcp_servers",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "server_id",
            "catalog_hash",
            "remote_name",
            name="uq_mcp_catalog_generation_tool",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text)
    server_id: Mapped[str] = mapped_column(Text)
    catalog_hash: Mapped[str] = mapped_column(String(64))
    remote_name: Mapped[str] = mapped_column(Text)
    registry_name: Mapped[str] = mapped_column(Text)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSONB)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryRow(Base):
    __tablename__ = "memories"
    __table_args__ = (
        Index(
            "ix_memories_principal_live_position",
            "tenant_id",
            "principal_id",
            "status",
            text("store_position DESC"),
        ),
        Index(
            "ix_memories_fts",
            text("to_tsvector('simple'::regconfig, (subject || ' '::text) || statement)"),
            postgresql_using="gin",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text)
    principal_id: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    statement: Mapped[str] = mapped_column(Text)
    source_session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.id", ondelete="RESTRICT")
    )
    source_event_ids: Mapped[list[int]] = mapped_column(JSONB)
    confidence: Mapped[float] = mapped_column(Float)
    sensitivity: Mapped[str] = mapped_column(Text)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text)
    belief_type: Mapped[str] = mapped_column(Text)
    polarity: Mapped[str] = mapped_column(Text)
    portability: Mapped[str] = mapped_column(Text)
    origin_scopes: Mapped[list[str]] = mapped_column(JSONB)
    corroboration_count: Mapped[int] = mapped_column(Integer)
    last_reinforced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("memories.id", ondelete="SET NULL")
    )
    conflicts_with: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    flagged_for_review: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    formation_run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    consolidation_policy_version: Mapped[str] = mapped_column(Text)
    authority: Mapped[str] = mapped_column(Text)
    utility: Mapped[float] = mapped_column(Float, server_default=text("0"))
    store_position: Mapped[int] = mapped_column(BigInteger, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MemoryRejectionRow(Base):
    __tablename__ = "memory_rejections"
    __table_args__ = (
        Index("ix_memory_rejections_principal", "tenant_id", "principal_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text)
    principal_id: Mapped[str] = mapped_column(Text)
    belief_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    statement: Mapped[str | None] = mapped_column(Text)
    statement_sha256: Mapped[str] = mapped_column(String(64))
    belief_type: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text)
    replacement_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    trace_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConsolidationRunRow(Base):
    __tablename__ = "consolidation_runs"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text)
    principal_id: Mapped[str] = mapped_column(Text)
    trigger: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text)
    session_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.id", ondelete="SET NULL")
    )
    watermark_before: Mapped[int] = mapped_column(BigInteger)
    watermark_after: Mapped[int] = mapped_column(BigInteger)
    model: Mapped[str] = mapped_column(Text)
    policy_version: Mapped[str] = mapped_column(Text)
    candidates_proposed: Mapped[int] = mapped_column(Integer)
    committed: Mapped[int] = mapped_column(Integer)
    reinforced: Mapped[int] = mapped_column(Integer)
    superseded: Mapped[int] = mapped_column(Integer)
    rejected: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConsolidationWatermarkRow(Base):
    __tablename__ = "consolidation_watermarks"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    principal_id: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RecallTraceRow(Base):
    __tablename__ = "recall_traces"
    __table_args__ = (
        Index("ix_recall_traces_turn", "turn_id", "created_at"),
        Index("ix_recall_traces_trace_gin", "trace", postgresql_using="gin"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text)
    principal_id: Mapped[str] = mapped_column(Text)
    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE")
    )
    turn_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    trace: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    operator_fields_expire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeDocumentRow(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "document_id", "version", name="uq_knowledge_document_version"
        ),
        Index(
            "ix_knowledge_documents_live",
            "tenant_id",
            "document_id",
            "valid_to",
        ),
    )

    row_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    document_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    tenant_id: Mapped[str] = mapped_column(Text)
    ingested_by_principal_id: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(Text)
    project_scope: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    source_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("artifacts.id", ondelete="RESTRICT")
    )
    media_type: Mapped[str] = mapped_column(Text)
    doc_date: Mapped[date | None] = mapped_column(Date)
    authority: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    chunker_version: Mapped[str] = mapped_column(Text)
    superseded_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("knowledge_documents.row_id", ondelete="SET NULL")
    )
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sensitivity: Mapped[str] = mapped_column(Text)


class KnowledgeChunkRow(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("document_row_id", "ordinal", name="uq_knowledge_chunk_document_ordinal"),
        Index(
            "ix_knowledge_chunks_fts",
            text("to_tsvector('simple'::regconfig, (heading_path::text || ' '::text) || text)"),
            postgresql_using="gin",
        ),
    )

    chunk_id: Mapped[str] = mapped_column(Text, primary_key=True)
    document_row_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("knowledge_documents.row_id", ondelete="CASCADE")
    )
    document_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    version: Mapped[int] = mapped_column(Integer)
    ordinal: Mapped[int] = mapped_column(Integer)
    heading_path: Mapped[list[str]] = mapped_column(JSONB)
    text: Mapped[str] = mapped_column(Text)
    tokens: Mapped[int] = mapped_column(Integer)
    contains_instruction_like_text: Mapped[bool] = mapped_column(Boolean)
    content_sha256: Mapped[str] = mapped_column(String(64))
