"""Principal-explicit lifecycle service for scheduled tasks."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from uuid import UUID

from agent_core.application.authorization import require_scope
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, ScheduleValidationError
from agent_core.domain.events import ProcessEvent
from agent_core.domain.recurrence import RecurrenceCalculator
from agent_core.domain.schedules import (
    OccurrenceCursor,
    Schedule,
    ScheduleCursor,
    ScheduleDefinition,
    ScheduleDefinitionLimits,
    ScheduleDefinitionPatch,
    ScheduleIdempotencyRecord,
    ScheduleOccurrence,
    SchedulePauseReason,
    ScheduleRecord,
    ScheduleRevision,
    ScheduleState,
)
from agent_core.domain.security import contains_credential
from agent_core.domain.views import Page
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

type WakeScheduleWorker = Callable[[], Awaitable[None]]
logger = logging.getLogger(__name__)


class ScheduleService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        limits: ScheduleDefinitionLimits,
        wake_worker: WakeScheduleWorker | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._limits = limits
        self._wake_worker = wake_worker

    async def create(
        self,
        principal: Principal,
        definition: ScheduleDefinition,
        idempotency_key: str,
    ) -> ScheduleRecord:
        require_scope(principal, "schedule.write")
        key = _idempotency_key(idempotency_key)
        request_hash = _definition_hash(definition)
        try:
            return await self._create_once(principal, definition, key, request_hash)
        except ConflictError:
            async with self._uow_factory() as uow:
                existing = await uow.schedule_idempotency.get(
                    principal.tenant_id, principal.principal_id, key
                )
                if existing is None or existing.request_hash != request_hash:
                    raise
                return await self._record(uow, principal, existing.schedule_id, replayed=True)

    async def _create_once(
        self,
        principal: Principal,
        definition: ScheduleDefinition,
        key: str,
        request_hash: str,
    ) -> ScheduleRecord:
        async with self._uow_factory() as uow:
            existing = await uow.schedule_idempotency.get(
                principal.tenant_id, principal.principal_id, key
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise ConflictError(
                        "schedule idempotency key was reused with different content",
                        reason="schedule.idempotency_mismatch",
                    )
                return await self._record(uow, principal, existing.schedule_id, replayed=True)
            await _validate_definition(uow, principal, definition, self._limits)
            now = self._clock.now()
            next_fire_at = RecurrenceCalculator.next_after(
                definition.cadence, now - timedelta(microseconds=1)
            )
            if next_fire_at is None:
                raise ScheduleValidationError(
                    "schedule.no_future_occurrence",
                    "schedule cadence has no occurrence at or after creation",
                )
            schedule_id = self._ids.new_id()
            schedule = Schedule(
                id=schedule_id,
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                state=ScheduleState.ACTIVE,
                current_revision=1,
                next_fire_at=next_fire_at,
                created_at=now,
                updated_at=now,
            )
            revision = _revision(schedule, definition, principal, now)
            await uow.schedules.create(schedule, revision)
            await uow.schedule_idempotency.create(
                ScheduleIdempotencyRecord(
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    key=key,
                    request_hash=request_hash,
                    schedule_id=schedule.id,
                    created_at=now,
                )
            )
            await self._event(uow, "schedule.created", principal, schedule, None)
        await self._wake()
        return ScheduleRecord(schedule=schedule, revision=revision)

    async def get(self, principal: Principal, schedule_id: UUID) -> ScheduleRecord:
        require_scope(principal, "schedule.read")
        async with self._uow_factory() as uow:
            return await self._record(uow, principal, schedule_id)

    async def list(
        self, principal: Principal, limit: int, cursor: str | None
    ) -> Page[ScheduleRecord]:
        require_scope(principal, "schedule.read")
        if limit <= 0:
            raise ValueError("schedule list limit must be positive")
        parsed = _decode_schedule_cursor(cursor)
        async with self._uow_factory() as uow:
            schedules = await uow.schedules.list(principal, limit=limit + 1, cursor=parsed)
            visible = schedules[:limit]
            revisions = await uow.schedules.get_revisions(
                tuple((schedule.id, schedule.current_revision) for schedule in visible),
                principal,
            )
            records = [
                ScheduleRecord(
                    schedule=schedule,
                    revision=revisions[(schedule.id, schedule.current_revision)],
                )
                for schedule in visible
            ]
        next_cursor = None
        if len(schedules) > limit and records:
            tail = records[-1].schedule
            next_cursor = _encode_cursor(
                {"updated_at": tail.updated_at.isoformat(), "id": str(tail.id)}
            )
        return Page(items=records, next_cursor=next_cursor)

    async def update(
        self,
        principal: Principal,
        schedule_id: UUID,
        expected_revision: int,
        definition: ScheduleDefinition,
    ) -> ScheduleRecord:
        require_scope(principal, "schedule.write")
        async with self._uow_factory() as uow:
            current = await uow.schedules.get(schedule_id, principal)
            _expected(current, expected_revision)
            if current.state in {ScheduleState.COMPLETED, ScheduleState.CANCELLED}:
                raise ConflictError(
                    "terminal schedule cannot be updated", reason="schedule.terminal"
                )
            await _validate_definition(uow, principal, definition, self._limits)
            now = self._clock.now()
            next_fire_at = (
                RecurrenceCalculator.next_after(definition.cadence, now)
                if current.state is ScheduleState.ACTIVE
                else None
            )
            if current.state is ScheduleState.ACTIVE and next_fire_at is None:
                raise ScheduleValidationError(
                    "schedule.no_future_occurrence",
                    "schedule cadence has no occurrence at or after update",
                )
            updated = current.model_copy(
                update={
                    "current_revision": current.current_revision + 1,
                    "next_fire_at": next_fire_at,
                    "updated_at": now,
                }
            )
            revision = _revision(updated, definition, principal, now)
            updated = await uow.schedules.replace(current, updated, revision)
            await self._event(uow, "schedule.updated", principal, updated, current)
        await self._wake()
        return ScheduleRecord(schedule=updated, revision=revision)

    async def patch(
        self,
        principal: Principal,
        schedule_id: UUID,
        expected_revision: int,
        patch: ScheduleDefinitionPatch,
        idempotency_key: str,
    ) -> ScheduleRecord:
        """Apply one model-visible patch without exposing hidden definition fields."""

        require_scope(principal, "schedule.write")
        key = _idempotency_key(idempotency_key)
        request_hash = _patch_hash(schedule_id, expected_revision, patch)
        try:
            record, changed = await self._patch_once(
                principal,
                schedule_id,
                expected_revision,
                patch,
                key,
                request_hash,
            )
        except ConflictError as exc:
            async with self._uow_factory() as uow:
                existing = await uow.schedule_idempotency.get(
                    principal.tenant_id, principal.principal_id, key
                )
                if existing is None:
                    raise
                if existing.request_hash != request_hash or existing.schedule_id != schedule_id:
                    raise _idempotency_mismatch() from exc
                return await self._record(uow, principal, schedule_id, replayed=True)
        if changed:
            await self._wake()
        return record

    async def _patch_once(
        self,
        principal: Principal,
        schedule_id: UUID,
        expected_revision: int,
        patch: ScheduleDefinitionPatch,
        key: str,
        request_hash: str,
    ) -> tuple[ScheduleRecord, bool]:
        async with self._uow_factory() as uow:
            existing = await uow.schedule_idempotency.get(
                principal.tenant_id, principal.principal_id, key
            )
            if existing is not None:
                if existing.request_hash != request_hash or existing.schedule_id != schedule_id:
                    raise _idempotency_mismatch()
                return (
                    await self._record(uow, principal, schedule_id, replayed=True),
                    False,
                )

            current = await uow.schedules.get(schedule_id, principal)
            _expected(current, expected_revision)
            if current.state in {ScheduleState.COMPLETED, ScheduleState.CANCELLED}:
                raise ConflictError(
                    "terminal schedule cannot be updated", reason="schedule.terminal"
                )
            current_revision = await uow.schedules.get_revision(
                schedule_id, current.current_revision, principal
            )
            current_definition = _definition_from_revision(current_revision)
            definition = current_definition.model_copy(
                update={
                    **({"title": patch.title} if patch.title is not None else {}),
                    **({"instruction": patch.instruction} if patch.instruction is not None else {}),
                    **({"cadence": patch.cadence} if patch.cadence is not None else {}),
                }
            )
            await _validate_definition(uow, principal, definition, self._limits)
            now = self._clock.now()
            if definition == current_definition:
                await uow.schedule_idempotency.create(
                    ScheduleIdempotencyRecord(
                        tenant_id=principal.tenant_id,
                        principal_id=principal.principal_id,
                        key=key,
                        request_hash=request_hash,
                        schedule_id=schedule_id,
                        created_at=now,
                    )
                )
                return ScheduleRecord(schedule=current, revision=current_revision), False

            next_fire_at = (
                RecurrenceCalculator.next_after(definition.cadence, now)
                if current.state is ScheduleState.ACTIVE
                else None
            )
            if current.state is ScheduleState.ACTIVE and next_fire_at is None:
                raise ScheduleValidationError(
                    "schedule.no_future_occurrence",
                    "schedule cadence has no occurrence after update",
                )
            updated = current.model_copy(
                update={
                    "current_revision": current.current_revision + 1,
                    "next_fire_at": next_fire_at,
                    "updated_at": now,
                }
            )
            revision = _revision(updated, definition, principal, now)
            updated = await uow.schedules.replace(current, updated, revision)
            await uow.schedule_idempotency.create(
                ScheduleIdempotencyRecord(
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    key=key,
                    request_hash=request_hash,
                    schedule_id=schedule_id,
                    created_at=now,
                )
            )
            await self._event(uow, "schedule.updated", principal, updated, current)
        return ScheduleRecord(schedule=updated, revision=revision), True

    async def pause(
        self, principal: Principal, schedule_id: UUID, expected_revision: int
    ) -> ScheduleRecord:
        require_scope(principal, "schedule.write")
        async with self._uow_factory() as uow:
            current = await uow.schedules.get(schedule_id, principal)
            _expected(current, expected_revision)
            if current.state is ScheduleState.PAUSED:
                return await self._record(uow, principal, schedule_id)
            if current.state is not ScheduleState.ACTIVE:
                raise ConflictError("schedule cannot be paused", reason="schedule.terminal")
            updated = current.model_copy(
                update={
                    "state": ScheduleState.PAUSED,
                    "pause_reason": SchedulePauseReason.USER,
                    "next_fire_at": None,
                    "updated_at": self._clock.now(),
                }
            )
            updated = await uow.schedules.replace(current, updated)
            revision = await uow.schedules.get_revision(
                schedule_id, updated.current_revision, principal
            )
            await self._event(uow, "schedule.paused", principal, updated, current)
        return ScheduleRecord(schedule=updated, revision=revision)

    async def resume(
        self, principal: Principal, schedule_id: UUID, expected_revision: int
    ) -> ScheduleRecord:
        require_scope(principal, "schedule.write")
        async with self._uow_factory() as uow:
            current = await uow.schedules.get(schedule_id, principal)
            _expected(current, expected_revision)
            if current.state is ScheduleState.ACTIVE:
                return await self._record(uow, principal, schedule_id)
            if current.state is not ScheduleState.PAUSED:
                raise ConflictError("schedule cannot be resumed", reason="schedule.terminal")
            revision = await uow.schedules.get_revision(
                schedule_id, current.current_revision, principal
            )
            now = self._clock.now()
            next_fire_at = RecurrenceCalculator.next_after(revision.cadence, now)
            if next_fire_at is None:
                raise ScheduleValidationError(
                    "schedule.no_future_occurrence",
                    "schedule has no future occurrence",
                )
            updated = current.model_copy(
                update={
                    "state": ScheduleState.ACTIVE,
                    "pause_reason": None,
                    "next_fire_at": next_fire_at,
                    "updated_at": now,
                }
            )
            updated = await uow.schedules.replace(current, updated)
            await self._event(uow, "schedule.resumed", principal, updated, current)
        await self._wake()
        return ScheduleRecord(schedule=updated, revision=revision)

    async def cancel(
        self, principal: Principal, schedule_id: UUID, expected_revision: int
    ) -> ScheduleRecord:
        require_scope(principal, "schedule.cancel")
        async with self._uow_factory() as uow:
            current = await uow.schedules.get(schedule_id, principal)
            _expected(current, expected_revision)
            if current.state is ScheduleState.CANCELLED:
                return await self._record(uow, principal, schedule_id)
            if current.state is ScheduleState.COMPLETED:
                raise ConflictError(
                    "completed schedule cannot be cancelled", reason="schedule.terminal"
                )
            updated = current.model_copy(
                update={
                    "state": ScheduleState.CANCELLED,
                    "pause_reason": None,
                    "next_fire_at": None,
                    "updated_at": self._clock.now(),
                }
            )
            updated = await uow.schedules.replace(current, updated)
            revision = await uow.schedules.get_revision(
                schedule_id, updated.current_revision, principal
            )
            await self._event(uow, "schedule.cancelled", principal, updated, current)
        return ScheduleRecord(schedule=updated, revision=revision)

    async def list_occurrences(
        self,
        principal: Principal,
        schedule_id: UUID,
        *,
        limit: int,
        cursor: str | None,
    ) -> Page[ScheduleOccurrence]:
        require_scope(principal, "schedule.read")
        if limit <= 0:
            raise ValueError("schedule occurrence limit must be positive")
        parsed = _decode_occurrence_cursor(cursor)
        async with self._uow_factory() as uow:
            values = await uow.schedule_occurrences.list(
                schedule_id, principal, limit=limit + 1, cursor=parsed
            )
        page = values[:limit]
        next_cursor = None
        if len(values) > limit and page:
            tail = page[-1]
            next_cursor = _encode_cursor(
                {"nominal_fire_at": tail.nominal_fire_at.isoformat(), "id": str(tail.id)}
            )
        return Page(items=page, next_cursor=next_cursor)

    async def _record(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        schedule_id: UUID,
        *,
        replayed: bool = False,
    ) -> ScheduleRecord:
        schedule = await uow.schedules.get(schedule_id, principal)
        revision = await uow.schedules.get_revision(
            schedule.id, schedule.current_revision, principal
        )
        return ScheduleRecord(schedule=schedule, revision=revision, replayed=replayed)

    async def _event(
        self,
        uow: RepositoryUnitOfWork,
        event_type: str,
        principal: Principal,
        schedule: Schedule,
        previous: Schedule | None,
    ) -> None:
        event_id = self._ids.new_id()
        await uow.process_events.append(
            ProcessEvent(
                id=event_id,
                event_type=event_type,
                actor_type="principal",
                actor_id=principal.principal_id,
                payload={
                    "schedule_id": str(schedule.id),
                    "revision": schedule.current_revision,
                    "tenant_id": schedule.tenant_id,
                    "principal_id": schedule.principal_id,
                    "previous_state": None if previous is None else previous.state.value,
                    "next_state": schedule.state.value,
                    "previous_next_fire_at": (
                        None
                        if previous is None or previous.next_fire_at is None
                        else previous.next_fire_at.isoformat()
                    ),
                    "next_fire_at": (
                        None if schedule.next_fire_at is None else schedule.next_fire_at.isoformat()
                    ),
                },
                derivation_key=f"{event_type}:{schedule.id}:{event_id}",
                created_at=self._clock.now(),
            )
        )

    async def _wake(self) -> None:
        if self._wake_worker is None:
            return
        try:
            await self._wake_worker()
        except Exception:
            logger.warning("schedule_worker_wakeup_failed", exc_info=True)


def _revision(
    schedule: Schedule,
    definition: ScheduleDefinition,
    principal: Principal,
    now: datetime,
) -> ScheduleRevision:
    return ScheduleRevision(
        schedule_id=schedule.id,
        revision=schedule.current_revision,
        title=definition.title,
        instruction=definition.instruction,
        agent_id=definition.agent_id,
        agent_version=definition.agent_version,
        policy_profile=definition.policy_profile,
        requested_scopes=definition.requested_scopes,
        limits=definition.limits,
        run_timeout_seconds=definition.run_timeout_seconds,
        cadence=definition.cadence,
        timezone=definition.timezone,
        misfire_grace_seconds=definition.misfire_grace_seconds,
        max_consecutive_failures=definition.max_consecutive_failures,
        created_by_principal_id=principal.principal_id,
        created_at=now,
    )


def _definition_from_revision(revision: ScheduleRevision) -> ScheduleDefinition:
    return ScheduleDefinition(
        title=revision.title,
        instruction=revision.instruction,
        agent_id=revision.agent_id,
        agent_version=revision.agent_version,
        policy_profile=revision.policy_profile,
        requested_scopes=revision.requested_scopes,
        limits=revision.limits,
        run_timeout_seconds=revision.run_timeout_seconds,
        cadence=revision.cadence,
        misfire_grace_seconds=revision.misfire_grace_seconds,
        max_consecutive_failures=revision.max_consecutive_failures,
    )


async def _validate_definition(
    uow: RepositoryUnitOfWork,
    principal: Principal,
    definition: ScheduleDefinition,
    limits: ScheduleDefinitionLimits,
) -> None:
    if not definition.requested_scopes <= principal.scopes:
        raise ScheduleValidationError(
            "schedule.scope_not_granted",
            "requested schedule scopes exceed the caller's authority",
        )
    if contains_credential(definition.instruction):
        raise ScheduleValidationError(
            "schedule.instruction_contains_credential",
            "schedule instruction failed credential validation",
        )
    if contains_credential(definition.title):
        raise ScheduleValidationError(
            "schedule.title_contains_credential",
            "schedule title failed credential validation",
        )
    ceiling_checks = (
        (
            definition.run_timeout_seconds,
            limits.max_run_timeout_seconds,
            "schedule.run_timeout_limit",
        ),
        (
            definition.misfire_grace_seconds,
            limits.max_misfire_grace_seconds,
            "schedule.misfire_grace_limit",
        ),
        (definition.limits.max_steps, limits.max_steps_per_run, "schedule.max_steps_limit"),
        (
            definition.limits.max_model_calls,
            limits.max_model_calls_per_run,
            "schedule.max_model_calls_limit",
        ),
        (
            definition.limits.max_tool_calls,
            limits.max_tool_calls_per_run,
            "schedule.max_tool_calls_limit",
        ),
    )
    for actual, maximum, reason in ceiling_checks:
        if actual > maximum:
            raise ScheduleValidationError(reason, "schedule definition exceeds tenant limits")
    if definition.limits.max_cost is None:
        raise ScheduleValidationError(
            "schedule.max_cost_limit",
            "schedule definition must set a finite cost bound",
        )
    if definition.limits.max_cost > limits.max_cost_per_run:
        raise ScheduleValidationError(
            "schedule.max_cost_limit", "schedule definition exceeds tenant limits"
        )
    agent = await uow.agents.get_version(definition.agent_id, definition.agent_version)
    if agent.policy_profile != definition.policy_profile:
        raise ScheduleValidationError(
            "schedule.policy_profile_mismatch",
            "schedule policy profile does not match the pinned agent version",
        )


def _expected(schedule: Schedule, expected_revision: int) -> None:
    if expected_revision != schedule.current_revision:
        raise ConflictError(
            "schedule revision changed",
            reason="schedule.revision_conflict",
            details={"current_revision": schedule.current_revision},
        )


def _idempotency_key(value: str) -> str:
    if not value or not value.strip() or len(value) > 255:
        raise ScheduleValidationError(
            "schedule.idempotency_key_invalid",
            "Idempotency-Key must contain 1 to 255 characters",
        )
    return value


def _definition_hash(definition: ScheduleDefinition) -> str:
    encoded = json.dumps(
        definition.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _patch_hash(
    schedule_id: UUID,
    expected_revision: int,
    patch: ScheduleDefinitionPatch,
) -> str:
    encoded = json.dumps(
        {
            "schedule_id": str(schedule_id),
            "expected_revision": expected_revision,
            "patch": patch.model_dump(mode="json", exclude_none=True),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _idempotency_mismatch() -> ConflictError:
    return ConflictError(
        "schedule idempotency key was reused with different content",
        reason="schedule.idempotency_mismatch",
    )


def _encode_cursor(value: dict[str, str]) -> str:
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(encoded).decode().rstrip("=")


def _decode_occurrence_cursor(value: str | None) -> OccurrenceCursor | None:
    if value is None:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode())
        return OccurrenceCursor.model_validate(decoded)
    except Exception as exc:
        raise ValueError("schedule occurrence cursor is malformed") from exc


def _decode_schedule_cursor(value: str | None) -> ScheduleCursor | None:
    if value is None:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode())
        return ScheduleCursor.model_validate(decoded)
    except Exception as exc:
        raise ValueError("schedule cursor is malformed") from exc
