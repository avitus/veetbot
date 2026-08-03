"""Hand-written translations between confined rows and domain values."""

from __future__ import annotations

from typing import Any, Literal, cast

from agent_core.adapters.persistence.sqlalchemy_models import (
    AgentRow,
    EventRow,
    IdempotencyKeyRow,
    ModelCallRow,
    RunRow,
    SessionRow,
    ToolInvocationRow,
)
from agent_core.adapters.persistence.upcasters import EventUpcasterRegistry
from agent_core.domain.agents import AgentSpec
from agent_core.domain.events import EventEnvelope, NewEvent
from agent_core.domain.messages import CostSource, ModelUsage, StopReason, ToolResultItem
from agent_core.domain.persistence import IdempotencyRecord, ModelCallRecord
from agent_core.domain.policies import IdempotencyClass, TrustLevel
from agent_core.domain.runs import Run, RunFailure, RunLimits, RunStatus, RunUsage
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.domain.tools import ToolInvocation, ToolInvocationStatus, ToolOutcome, ToolSource


def agent_to_domain(row: AgentRow) -> AgentSpec:
    return AgentSpec(
        id=row.id,
        version=row.version,
        name=row.name,
        instructions=row.instructions,
        model_policy=row.model_policy,
        enabled_tools=list(row.enabled_tools),
        enabled_skills=list(row.enabled_skills),
        policy_profile=row.policy_profile,
        limits=RunLimits.model_validate(row.limits),
        metadata=dict(row.metadata_json),
    )


def agent_values(agent: AgentSpec, *, created_at: object) -> dict[str, Any]:
    return {
        "id": agent.id,
        "version": agent.version,
        "name": agent.name,
        "instructions": agent.instructions,
        "model_policy": agent.model_policy,
        "enabled_tools": agent.enabled_tools,
        "enabled_skills": agent.enabled_skills,
        "policy_profile": agent.policy_profile,
        "limits": agent.limits.model_dump(mode="json"),
        "metadata_json": agent.metadata,
        "created_at": created_at,
    }


def session_to_domain(row: SessionRow) -> Session:
    return Session(
        id=row.id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        agent_id=row.agent_id,
        agent_version=row.agent_version,
        status=SessionStatus(row.status),
        title=row.title,
        metadata=dict(row.metadata_json),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def session_values(session: Session) -> dict[str, Any]:
    return {
        "id": session.id,
        "tenant_id": session.tenant_id,
        "principal_id": session.principal_id,
        "agent_id": session.agent_id,
        "agent_version": session.agent_version,
        "status": session.status.value,
        "title": session.title,
        "metadata_json": session.metadata,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def run_to_domain(row: RunRow) -> Run:
    return Run(
        id=row.id,
        session_id=row.session_id,
        parent_run_id=row.parent_run_id,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        agent_version=row.agent_version,
        status=RunStatus(row.status),
        step_count=row.step_count,
        model_call_count=row.model_call_count,
        tool_call_count=row.tool_call_count,
        limits=RunLimits.model_validate(row.limits),
        usage=RunUsage.model_validate(row.usage),
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
        lease_epoch=row.lease_epoch,
        attempts=row.attempts,
        priority=row.priority,
        scheduled_for=row.scheduled_for,
        deadline_at=row.deadline_at,
        cancel_requested_at=row.cancel_requested_at,
        failure=None if row.failure is None else RunFailure.model_validate(row.failure),
        final_message=row.final_message,
        seed_event_sequence=row.seed_event_sequence,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def run_values(run: Run) -> dict[str, Any]:
    return {
        "id": run.id,
        "session_id": run.session_id,
        "parent_run_id": run.parent_run_id,
        "tenant_id": run.tenant_id,
        "agent_id": run.agent_id,
        "agent_version": run.agent_version,
        "status": run.status.value,
        "step_count": run.step_count,
        "model_call_count": run.model_call_count,
        "tool_call_count": run.tool_call_count,
        "limits": run.limits.model_dump(mode="json"),
        "usage": run.usage.model_dump(mode="json"),
        "lease_owner": run.lease_owner,
        "lease_expires_at": run.lease_expires_at,
        "lease_epoch": run.lease_epoch,
        "attempts": run.attempts,
        "priority": run.priority,
        "scheduled_for": run.scheduled_for,
        "deadline_at": run.deadline_at,
        "cancel_requested_at": run.cancel_requested_at,
        "failure": None if run.failure is None else run.failure.model_dump(mode="json"),
        "final_message": run.final_message,
        "seed_event_sequence": run.seed_event_sequence,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def event_to_domain(row: EventRow, upcasters: EventUpcasterRegistry) -> EventEnvelope:
    version, payload = upcasters.upcast(row.event_type, row.payload_schema_version, row.payload)
    return EventEnvelope(
        id=row.id,
        session_id=row.session_id,
        run_id=row.run_id,
        sequence=row.sequence,
        event_type=row.event_type,
        payload_schema_version=version,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        payload=payload,
        trace_id=row.trace_id,
        derivation_key=row.derivation_key,
        created_at=row.created_at,
    )


def event_values(event: NewEvent, *, sequence: int, created_at: object) -> dict[str, Any]:
    return {
        "session_id": event.session_id,
        "run_id": event.run_id,
        "sequence": sequence,
        "event_type": event.event_type,
        "payload_schema_version": event.payload_schema_version,
        "actor_type": event.actor_type,
        "actor_id": event.actor_id,
        "payload": event.payload,
        "trace_id": event.trace_id,
        "derivation_key": event.derivation_key,
        "created_at": created_at,
    }


def invocation_to_domain(row: ToolInvocationRow) -> ToolInvocation:
    return ToolInvocation(
        id=row.id,
        run_id=row.run_id,
        session_id=row.session_id,
        step_number=row.step_number,
        call_id=row.provider_call_id,
        tool_name=row.tool_name,
        tool_version=row.tool_version,
        tool_source=ToolSource(row.tool_source),
        server_id=row.server_id,
        idempotency_class=IdempotencyClass(row.idempotency_class),
        attempt_number=row.attempt_number,
        status=ToolInvocationStatus(row.status),
        raw_arguments=row.raw_arguments,
        normalized_arguments=None if row.arguments is None else dict(row.arguments),
        normalized_arguments_hash=row.normalized_arguments_hash,
        idempotency_key=row.idempotency_key,
        effect_sent_at=row.effect_sent_at,
        suspended_kind=row.suspended_kind,
        suspended_ref=row.suspended_ref,
        output_bytes=row.output_bytes,
        truncated=row.truncated,
        artifact_id=row.artifact_id,
        origin_trust=TrustLevel(row.origin_trust),
        parallel_group=row.parallel_group,
        outcome=None if row.outcome is None else ToolOutcome.model_validate(row.outcome),
        result_item=(
            None if row.result_item is None else ToolResultItem.model_validate(row.result_item)
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def invocation_values(invocation: ToolInvocation) -> dict[str, Any]:
    return {
        "id": invocation.id,
        "run_id": invocation.run_id,
        "session_id": invocation.session_id,
        "step_number": invocation.step_number,
        "provider_call_id": invocation.call_id,
        "tool_name": invocation.tool_name,
        "tool_version": invocation.tool_version,
        "tool_source": invocation.tool_source.value,
        "server_id": invocation.server_id,
        "idempotency_class": invocation.idempotency_class.value,
        "attempt_number": invocation.attempt_number,
        "raw_arguments": invocation.raw_arguments,
        "arguments": invocation.normalized_arguments,
        "normalized_arguments_hash": invocation.normalized_arguments_hash,
        "idempotency_key": invocation.idempotency_key,
        "status": invocation.status.value,
        "effect_sent_at": invocation.effect_sent_at,
        "suspended_kind": invocation.suspended_kind,
        "suspended_ref": invocation.suspended_ref,
        "output_bytes": invocation.output_bytes,
        "truncated": invocation.truncated,
        "artifact_id": invocation.artifact_id,
        "outcome_status": (None if invocation.outcome is None else invocation.outcome.status.value),
        "reason_code": None if invocation.outcome is None else invocation.outcome.reason_code,
        "origin_trust": invocation.origin_trust.value,
        "parallel_group": invocation.parallel_group,
        "outcome": None
        if invocation.outcome is None
        else invocation.outcome.model_dump(mode="json"),
        "result_item": (
            None
            if invocation.result_item is None
            else invocation.result_item.model_dump(mode="json")
        ),
        "created_at": invocation.created_at,
        "updated_at": invocation.updated_at,
    }


def idempotency_to_domain(row: IdempotencyKeyRow) -> IdempotencyRecord:
    return IdempotencyRecord(
        key=row.key,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        request_hash=row.request_hash,
        run_id=row.run_id,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


def idempotency_values(record: IdempotencyRecord) -> dict[str, Any]:
    return record.model_dump()


def model_call_to_domain(row: ModelCallRow) -> ModelCallRecord:
    return ModelCallRecord(
        attempt_id=row.attempt_id,
        run_id=row.run_id,
        session_id=row.session_id,
        tenant_id=row.tenant_id,
        step_number=row.step_number,
        attempt_number=row.attempt_number,
        provider=row.provider,
        model=row.model,
        model_policy=row.model_policy,
        registry_version=row.registry_version,
        prefix_sha256=row.prefix_sha256,
        usage=ModelUsage(
            input_tokens=row.input_tokens,
            cached_input_tokens=row.cached_input_tokens,
            cache_write_input_tokens=row.cache_write_tokens,
            output_tokens=row.output_tokens,
            reasoning_tokens=row.reasoning_tokens,
            cost=row.cost,
            cost_source=CostSource(row.cost_source),
            provider=row.provider,
            model=row.model,
        ),
        cost=row.cost,
        cost_source=CostSource(row.cost_source),
        price_id=row.price_id,
        stop_reason=None if row.stop_reason is None else StopReason(row.stop_reason),
        error_kind=cast(
            Literal["transient", "permanent", "protocol"] | None,
            row.error_kind,
        ),
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def model_call_values(call: ModelCallRecord) -> dict[str, Any]:
    return {
        "attempt_id": call.attempt_id,
        "run_id": call.run_id,
        "session_id": call.session_id,
        "tenant_id": call.tenant_id,
        "step_number": call.step_number,
        "attempt_number": call.attempt_number,
        "provider": call.provider,
        "model": call.model,
        "model_policy": call.model_policy,
        "registry_version": call.registry_version,
        "prefix_sha256": call.prefix_sha256,
        "input_tokens": call.usage.input_tokens,
        "cached_input_tokens": call.usage.cached_input_tokens,
        "cache_write_tokens": call.usage.cache_write_input_tokens,
        "output_tokens": call.usage.output_tokens,
        "reasoning_tokens": call.usage.reasoning_tokens,
        "cost": call.cost,
        "cost_source": call.cost_source.value,
        "price_id": call.price_id,
        "stop_reason": None if call.stop_reason is None else call.stop_reason.value,
        "error_kind": call.error_kind,
        "started_at": call.started_at,
        "finished_at": call.finished_at,
    }
