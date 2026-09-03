"""Hand-written translations between confined rows and domain values."""

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import SecretStr

from agent_core.adapters.persistence.sqlalchemy_models import (
    AgentRow,
    ApprovalRow,
    ArtifactRow,
    DelegationRow,
    DeviceIngestReceiptRow,
    DeviceInvocationRow,
    DeviceRegistrationIdempotencyRow,
    DeviceRow,
    DeviceTriageSessionRow,
    EventRow,
    IdempotencyKeyRow,
    ModelCallRow,
    NotificationDeliveryRow,
    NotificationOutboxRow,
    RunRow,
    ScheduleIdempotencyKeyRow,
    ScheduleOccurrenceRow,
    ScheduleRevisionRow,
    ScheduleRow,
    SessionRow,
    ToolInvocationRow,
    TrajectoryExportRow,
)
from agent_core.adapters.persistence.upcasters import EventUpcasterRegistry
from agent_core.domain.agents import AgentSpec
from agent_core.domain.approvals import ApprovalRequest
from agent_core.domain.delegations import Delegation
from agent_core.domain.devices import (
    Device,
    DeviceIngestReceipt,
    DeviceInvocation,
    DeviceInvocationStatus,
    DeviceKind,
    DeviceRegistrationIdempotencyRecord,
    DeviceStatus,
    DeviceTriageMapping,
    PushEnvironment,
    PushProvider,
)
from agent_core.domain.events import EventEnvelope, NewEvent
from agent_core.domain.messages import (
    CostSource,
    ModelUsage,
    ProviderMetadata,
    ProviderPin,
    StopReason,
    ToolResultItem,
)
from agent_core.domain.notifications import (
    DeliveryOutcome,
    NewNotification,
    Notification,
    NotificationDelivery,
    NotificationKind,
    NotificationPayload,
    NotificationStatus,
)
from agent_core.domain.persistence import IdempotencyRecord, ModelCallRecord
from agent_core.domain.policies import (
    IdempotencyClass,
    PolicyDecision,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.runs import Run, RunFailure, RunKind, RunLimits, RunStatus, RunUsage
from agent_core.domain.schedules import (
    OccurrenceDisposition,
    Schedule,
    ScheduleIdempotencyRecord,
    ScheduleOccurrence,
    SchedulePauseReason,
    ScheduleRevision,
    ScheduleState,
)
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.domain.tools import ToolInvocation, ToolInvocationStatus, ToolOutcome, ToolSource
from agent_core.domain.trajectory import ArtifactRef, TrajectoryExport


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
        kind=RunKind(row.run_kind),
        tenant_id=row.tenant_id,
        principal_scopes=set(row.principal_scopes),
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
        export_consent=row.export_consent,
        provider_pin=(
            None if row.provider_pin is None else ProviderPin.model_validate(row.provider_pin)
        ),
        seed_event_sequence=row.seed_event_sequence,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def run_values(run: Run) -> dict[str, Any]:
    values = {
        "id": run.id,
        "session_id": run.session_id,
        "parent_run_id": run.parent_run_id,
        "run_kind": run.kind.value,
        "tenant_id": run.tenant_id,
        "principal_scopes": sorted(run.principal_scopes),
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
        "export_consent": run.export_consent,
        "provider_pin": (
            None if run.provider_pin is None else run.provider_pin.model_dump(mode="json")
        ),
        "seed_event_sequence": run.seed_event_sequence,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }
    if run.provider_pin is None:
        values.pop("provider_pin")
    return values


def schedule_to_domain(row: ScheduleRow) -> Schedule:
    return Schedule(
        id=row.id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        state=ScheduleState(row.state),
        pause_reason=None if row.pause_reason is None else SchedulePauseReason(row.pause_reason),
        current_revision=row.current_revision,
        next_fire_at=row.next_fire_at,
        consecutive_failures=row.consecutive_failures,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def schedule_values(schedule: Schedule) -> dict[str, Any]:
    return {
        "id": schedule.id,
        "tenant_id": schedule.tenant_id,
        "principal_id": schedule.principal_id,
        "state": schedule.state.value,
        "pause_reason": None if schedule.pause_reason is None else schedule.pause_reason.value,
        "current_revision": schedule.current_revision,
        "next_fire_at": schedule.next_fire_at,
        "consecutive_failures": schedule.consecutive_failures,
        "created_at": schedule.created_at,
        "updated_at": schedule.updated_at,
    }


def device_to_domain(row: DeviceRow) -> Device:
    return Device(
        id=row.id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        client_device_id=row.client_device_id,
        name=row.name,
        kind=DeviceKind(row.kind),
        platform=row.platform,
        app_bundle_id=row.app_bundle_id,
        push_provider=None if row.push_provider is None else PushProvider(row.push_provider),
        push_token=None if row.push_token is None else SecretStr(row.push_token),
        push_environment=(
            None if row.push_environment is None else PushEnvironment(row.push_environment)
        ),
        push_token_updated_at=row.push_token_updated_at,
        push_token_invalidated_at=row.push_token_invalidated_at,
        muted_kinds=frozenset(NotificationKind(value) for value in row.muted_kinds),
        capabilities=frozenset(row.capabilities),
        status=DeviceStatus(row.status),
        revoked_at=row.revoked_at,
        last_seen_at=row.last_seen_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def device_values(device: Device) -> dict[str, Any]:
    return {
        "id": device.id,
        "tenant_id": device.tenant_id,
        "principal_id": device.principal_id,
        "client_device_id": device.client_device_id,
        "name": device.name,
        "kind": device.kind.value,
        "platform": device.platform,
        "app_bundle_id": device.app_bundle_id,
        "push_provider": None if device.push_provider is None else device.push_provider.value,
        "push_token": (None if device.push_token is None else device.push_token.get_secret_value()),
        "push_environment": (
            None if device.push_environment is None else device.push_environment.value
        ),
        "push_token_updated_at": device.push_token_updated_at,
        "push_token_invalidated_at": device.push_token_invalidated_at,
        "muted_kinds": sorted(kind.value for kind in device.muted_kinds),
        "capabilities": sorted(device.capabilities),
        "status": device.status.value,
        "revoked_at": device.revoked_at,
        "last_seen_at": device.last_seen_at,
        "created_at": device.created_at,
        "updated_at": device.updated_at,
    }


def notification_to_domain(row: NotificationOutboxRow) -> Notification:
    return Notification(
        id=row.id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        kind=NotificationKind(row.kind),
        dedupe_key=row.dedupe_key,
        session_id=row.session_id,
        run_id=row.run_id,
        approval_id=row.approval_id,
        question_id=row.question_id,
        schedule_id=row.schedule_id,
        occurrence_id=row.occurrence_id,
        payload=NotificationPayload.model_validate(row.payload),
        priority=row.priority,
        expires_at=row.expires_at,
        status=NotificationStatus(row.status),
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        claimed_by=row.claimed_by,
        claimed_until=row.claimed_until,
        created_at=row.created_at,
        settled_at=row.settled_at,
    )


def new_notification_values(notification: NewNotification) -> dict[str, Any]:
    return {
        **notification.model_dump(exclude={"payload"}),
        "kind": notification.kind.value,
        "payload": notification.payload.model_dump(mode="json"),
        "status": NotificationStatus.PENDING.value,
        "attempts": 0,
        "claimed_by": None,
        "claimed_until": None,
        "settled_at": None,
    }


def notification_delivery_values(delivery: NotificationDelivery) -> dict[str, Any]:
    return {
        **delivery.model_dump(),
        "outcome": delivery.outcome.value,
    }


def notification_delivery_to_domain(row: NotificationDeliveryRow) -> NotificationDelivery:
    return NotificationDelivery(
        id=row.id,
        notification_id=row.notification_id,
        device_id=row.device_id,
        attempt=row.attempt,
        outcome=DeliveryOutcome(row.outcome),
        provider_reason=row.provider_reason,
        provider_id=row.provider_id,
        attempted_at=row.attempted_at,
    )


def schedule_revision_to_domain(row: ScheduleRevisionRow) -> ScheduleRevision:
    return ScheduleRevision.model_validate(
        {
            "schedule_id": row.schedule_id,
            "revision": row.revision,
            **row.definition,
            "created_by_principal_id": row.created_by_principal_id,
            "created_at": row.created_at,
        }
    )


def schedule_revision_values(revision: ScheduleRevision) -> dict[str, Any]:
    definition = revision.model_dump(
        mode="json",
        exclude={"schedule_id", "revision", "created_by_principal_id", "created_at"},
    )
    definition["requested_scopes"] = sorted(revision.requested_scopes)
    return {
        "schedule_id": revision.schedule_id,
        "revision": revision.revision,
        "definition": definition,
        "created_by_principal_id": revision.created_by_principal_id,
        "created_at": revision.created_at,
    }


def schedule_occurrence_to_domain(row: ScheduleOccurrenceRow) -> ScheduleOccurrence:
    return ScheduleOccurrence(
        id=row.id,
        schedule_id=row.schedule_id,
        schedule_revision=row.schedule_revision,
        nominal_fire_at=row.nominal_fire_at,
        disposition=OccurrenceDisposition(row.disposition),
        session_id=row.session_id,
        run_id=row.run_id,
        reason_code=row.reason_code,
        authority_version=row.authority_version,
        materialized_at=row.materialized_at,
        links_erased_at=row.links_erased_at,
        created_at=row.created_at,
    )


def schedule_occurrence_values(occurrence: ScheduleOccurrence) -> dict[str, Any]:
    values = occurrence.model_dump(mode="python")
    values["disposition"] = occurrence.disposition.value
    return values


def schedule_idempotency_to_domain(row: ScheduleIdempotencyKeyRow) -> ScheduleIdempotencyRecord:
    return ScheduleIdempotencyRecord.model_validate(
        {key: getattr(row, key) for key in ScheduleIdempotencyRecord.model_fields}
    )


def schedule_idempotency_values(record: ScheduleIdempotencyRecord) -> dict[str, Any]:
    return record.model_dump(mode="python")


def device_registration_idempotency_to_domain(
    row: DeviceRegistrationIdempotencyRow,
) -> DeviceRegistrationIdempotencyRecord:
    return DeviceRegistrationIdempotencyRecord.model_validate(
        {key: getattr(row, key) for key in DeviceRegistrationIdempotencyRecord.model_fields}
    )


def device_registration_idempotency_values(
    record: DeviceRegistrationIdempotencyRecord,
) -> dict[str, Any]:
    return record.model_dump(mode="python")


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
        side_effect=SideEffectClass(row.side_effect),
        risk=RiskLevel(row.risk),
        attempt_number=row.attempt_number,
        status=ToolInvocationStatus(row.status),
        raw_arguments=row.raw_arguments,
        normalized_arguments=None if row.arguments is None else dict(row.arguments),
        normalized_arguments_hash=row.normalized_arguments_hash,
        effective_arguments_hash=row.effective_arguments_hash,
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
        policy_decision=(
            None
            if row.policy_decision is None
            else PolicyDecision.model_validate(row.policy_decision)
        ),
        structured_result=(None if row.structured_result is None else dict(row.structured_result)),
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
        "side_effect": invocation.side_effect.value,
        "risk": invocation.risk.value,
        "attempt_number": invocation.attempt_number,
        "raw_arguments": invocation.raw_arguments,
        "arguments": invocation.normalized_arguments,
        "normalized_arguments_hash": invocation.normalized_arguments_hash,
        "effective_arguments_hash": invocation.effective_arguments_hash,
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
        "structured_result": invocation.structured_result,
        "policy_decision": (
            None
            if invocation.policy_decision is None
            else invocation.policy_decision.model_dump(mode="json")
        ),
        "created_at": invocation.created_at,
        "updated_at": invocation.updated_at,
    }


def approval_to_domain(row: ApprovalRow) -> ApprovalRequest:
    data = dict(row.request)
    data.update(
        {
            "status": row.status,
            "resolution": (None if row.resolution is None else row.resolution.get("resolution")),
            "resolution_reason": (None if row.resolution is None else row.resolution.get("reason")),
            "revalidated_policy_version": row.revalidated_policy_version,
            "resolved_at": row.resolved_at,
            "resolved_by": row.resolved_by,
        }
    )
    return ApprovalRequest.model_validate(data)


def approval_values(request: ApprovalRequest) -> dict[str, Any]:
    return {
        "id": request.id,
        "tenant_id": request.tenant_id,
        "principal_id": request.principal_id,
        "session_id": request.session_id,
        "run_id": request.run_id,
        "action_kind": request.action_kind.value,
        "action_id": request.action_id,
        "tool_invocation_id": request.tool_invocation_id,
        "risk": request.risk.value,
        "policy_version": request.policy_version,
        "revalidated_policy_version": request.revalidated_policy_version,
        "status": request.status.value,
        "request": request.model_dump(mode="json"),
        "resolution": (
            None
            if request.resolution is None
            else {"resolution": request.resolution.value, "reason": request.resolution_reason}
        ),
        "expires_at": request.expires_at,
        "created_at": request.created_at,
        "resolved_at": request.resolved_at,
        "resolved_by": request.resolved_by,
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
        metadata=ProviderMetadata(
            provider_api=cast(
                Literal["responses", "messages", "chat_completions"],
                row.provider_api,
            ),
            response_id=row.response_id,
            request_id=row.request_id,
            resolved_model=row.resolved_model,
            cache_breakpoints_sent=row.cache_breakpoints_sent,
            cache_breakpoints_dropped=row.cache_breakpoints_dropped,
        ),
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def flatten_provider_metadata(
    metadata: ProviderMetadata | None,
    *,
    provider: str,
) -> dict[str, Any]:
    """Flatten the closed metadata model into migration-controlled columns."""

    if metadata is None:
        provider_api = (
            "responses"
            if provider == "openai"
            else "messages"
            if provider == "anthropic"
            else "chat_completions"
        )
        values: dict[str, Any] = {"provider_api": provider_api}
    else:
        values = metadata.model_dump(mode="python", exclude_none=False)
    values.pop("previous_response_id", None)
    return values


def model_call_values(call: ModelCallRecord) -> dict[str, Any]:
    values = {
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
    values.update(flatten_provider_metadata(call.metadata, provider=call.provider))
    return values


def artifact_to_domain(row: ArtifactRow) -> ArtifactRef:
    return ArtifactRef(
        id=row.id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        session_id=row.session_id,
        run_id=row.run_id,
        name=row.name,
        media_type=row.media_type,
        storage_uri=row.storage_uri,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        origin=cast(Any, row.origin),
        trust=TrustLevel(row.trust),
        expires_at=row.expires_at,
        created_at=row.created_at,
        metadata=dict(row.metadata_json),
    )


def artifact_values(artifact: ArtifactRef) -> dict[str, Any]:
    values = artifact.model_dump(mode="python")
    values["metadata_json"] = values.pop("metadata")
    return values


def trajectory_export_to_domain(
    row: TrajectoryExportRow, artifact: ArtifactRow
) -> TrajectoryExport:
    return TrajectoryExport(
        export_id=row.export_id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        run_id=row.run_id,
        artifact=artifact_to_domain(artifact),
        builder_version=row.builder_version,
        ruleset_version=row.ruleset_version,
        created_at=row.created_at,
    )


def trajectory_export_values(export: TrajectoryExport) -> dict[str, Any]:
    return {
        "export_id": export.export_id,
        "tenant_id": export.tenant_id,
        "principal_id": export.principal_id,
        "run_id": export.run_id,
        "artifact_id": export.artifact.id,
        "builder_version": export.builder_version,
        "ruleset_version": export.ruleset_version,
        "created_at": export.created_at,
    }


def delegation_to_domain(row: DelegationRow) -> Delegation:
    return Delegation.model_validate(
        {
            "id": row.id,
            "tenant_id": row.tenant_id,
            "principal_id": row.principal_id,
            "parent_run_id": row.parent_run_id,
            "parent_session_id": row.parent_session_id,
            "invocation_id": row.invocation_id,
            "depth": row.depth,
            "request": row.brief,
            "derived_limits": row.derived_limits,
            "granted_scopes": row.granted_scopes,
            "status": row.status,
            "children": row.children,
            "result": row.result,
            "links_erased_at": row.links_erased_at,
            "created_at": row.created_at,
            "joined_at": row.joined_at,
        }
    )


def delegation_values(delegation: Delegation) -> dict[str, Any]:
    return {
        "id": delegation.id,
        "tenant_id": delegation.tenant_id,
        "principal_id": delegation.principal_id,
        "parent_run_id": delegation.parent_run_id,
        "parent_session_id": delegation.parent_session_id,
        "invocation_id": delegation.invocation_id,
        "depth": delegation.depth,
        "brief": delegation.request.model_dump(mode="json"),
        "derived_limits": [limits.model_dump(mode="json") for limits in delegation.derived_limits],
        "granted_scopes": [sorted(scopes) for scopes in delegation.granted_scopes],
        "status": delegation.status.value,
        "children": [child.model_dump(mode="json") for child in delegation.children],
        "result": None if delegation.result is None else delegation.result.model_dump(mode="json"),
        "links_erased_at": delegation.links_erased_at,
        "created_at": delegation.created_at,
        "joined_at": delegation.joined_at,
    }


def device_invocation_to_domain(row: DeviceInvocationRow) -> DeviceInvocation:
    return DeviceInvocation(
        id=row.id,
        tenant_id=row.tenant_id,
        device_id=row.device_id,
        run_id=row.run_id,
        tool_name=row.tool_name,
        arguments=row.arguments,
        status=DeviceInvocationStatus(row.status),
        created_at=row.created_at,
        resolved_at=row.resolved_at,
    )


def device_invocation_values(invocation: DeviceInvocation) -> dict[str, Any]:
    return {
        "id": invocation.id,
        "tenant_id": invocation.tenant_id,
        "device_id": invocation.device_id,
        "run_id": invocation.run_id,
        "tool_name": invocation.tool_name,
        "arguments": invocation.arguments,
        "status": invocation.status.value,
        "created_at": invocation.created_at,
        "resolved_at": invocation.resolved_at,
    }


def device_ingest_receipt_to_domain(row: DeviceIngestReceiptRow) -> DeviceIngestReceipt:
    return DeviceIngestReceipt(
        device_id=row.device_id,
        tenant_id=row.tenant_id,
        channel=row.channel,
        digest=row.digest,
        received_at=row.received_at,
        accepted_at=row.accepted_at,
        session_id=row.session_id,
        run_id=row.run_id,
    )


def device_ingest_receipt_values(receipt: DeviceIngestReceipt) -> dict[str, Any]:
    return {
        "device_id": receipt.device_id,
        "tenant_id": receipt.tenant_id,
        "channel": receipt.channel,
        "digest": receipt.digest,
        "received_at": receipt.received_at,
        "accepted_at": receipt.accepted_at,
        "session_id": receipt.session_id,
        "run_id": receipt.run_id,
    }


def device_triage_mapping_to_domain(row: DeviceTriageSessionRow) -> DeviceTriageMapping:
    return DeviceTriageMapping(
        device_id=row.device_id,
        tenant_id=row.tenant_id,
        channel=row.channel,
        session_id=row.session_id,
    )


def device_triage_mapping_values(mapping: DeviceTriageMapping) -> dict[str, Any]:
    return {
        "device_id": mapping.device_id,
        "tenant_id": mapping.tenant_id,
        "channel": mapping.channel,
        "session_id": mapping.session_id,
    }
