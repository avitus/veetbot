"""Transactional pairing and inbound routing shared by messaging adapters."""

from __future__ import annotations

import builtins
import inspect
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from agent_core.application.authorization import require_scope
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalResolutionState, ApprovalResolutionType
from agent_core.domain.devices import Device, DeviceKind, PushProvider, push_token_fingerprint
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.events import NewEvent, ProcessEvent
from agent_core.domain.runs import Run, RunStatus
from agent_core.domain.sessions import Session
from agent_core.domain.surfaces import (
    InboundDisposition,
    InboundReceipt,
    IssuedPairingCode,
    Pairing,
    SurfaceChatKind,
    SurfaceInboundMessage,
    SurfaceMessageKind,
    SurfaceSession,
    direct_message_key,
    issue_pairing_code,
    pairing_code_matches,
    rotation_reason,
)
from agent_core.domain.views import DeviceView
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory
from agent_core.ports.schedules import SchedulePrincipalDirectory
from agent_core.ports.surfaces import SurfaceAdmissionController

type SurfaceSessionCreator = Callable[
    [RepositoryUnitOfWork, Principal, SurfaceInboundMessage], Awaitable[Session]
]
type SurfaceSubmitter = Callable[
    [
        RepositoryUnitOfWork,
        Principal,
        Session,
        str,
        dict[str, object],
        str,
    ],
    Awaitable[PreparedSurfaceSubmission],
]
type SurfaceDispatch = Callable[[PreparedSurfaceSubmission], Awaitable[None] | None]
type SurfaceNotice = Callable[[str, str], Awaitable[None]]
type SurfaceStopRun = Callable[[RepositoryUnitOfWork, Run, str], Awaitable[Run]]
type SurfaceResumeApproval = Callable[[RepositoryUnitOfWork, Run], Awaitable[Run]]

_NOTICE_TEXT: dict[str, str] = {
    "surface.active_run": "Still working; /stop to cancel.",
    "surface.approval_already_resolved": "That approval was already resolved.",
    "surface.approval_not_found": "That pending approval was not found.",
    "surface.approval_resolved": "Approval resolved.",
    "surface.chat_kind_unsupported": "Veetbot accepts direct messages only.",
    "surface.daily_cost_limit": "The daily surface budget is exhausted.",
    "surface.help": (
        "Send a message, or use /new, /stop, /status, /approve ID, /deny ID, or /help."
    ),
    "surface.media_unsupported": "Veetbot accepts text messages only.",
    "surface.monthly_cost_limit": "The monthly surface budget is exhausted.",
    "surface.no_active_run": "There is no active run to stop.",
    "surface.paired": "Pairing complete. You can now message Veetbot.",
    "surface.pairing_invalid": "That pairing code is invalid or expired.",
    "surface.pairing_locked": "Pairing is temporarily locked for this sender.",
    "surface.principal_unavailable": "The paired Veetbot identity is unavailable.",
    "surface.rate_limited": "Too many messages; wait before trying again.",
    "surface.run_stopped": "The active run was stopped.",
    "surface.scope_denied": "This pairing does not allow that action.",
    "surface.session_rotated": "A new chat session will start with your next message.",
    "surface.status": "Veetbot is connected and this pairing is active.",
    "surface.text_too_large": "That message is too large.",
    "surface.unpaired": "This sender is not paired. Send /pair followed by a code.",
}


def surface_notice_text(reason_code: str) -> str:
    """Render a closed reason code without provider or user content."""

    return _NOTICE_TEXT.get(reason_code, "Veetbot could not process that message.")


def _surface_view(device: Device) -> DeviceView:
    fingerprint = (
        None
        if device.push_token is None
        else push_token_fingerprint(device.push_token.get_secret_value())
    )
    return DeviceView(
        id=device.id,
        client_device_id=device.client_device_id,
        name=device.name,
        kind=device.kind,
        platform=device.platform,
        app_bundle_id=device.app_bundle_id,
        push_provider=device.push_provider,
        push_environment=device.push_environment,
        push_token_fingerprint=fingerprint,
        push_token_updated_at=device.push_token_updated_at,
        push_token_invalidated_at=device.push_token_invalidated_at,
        muted_kinds=device.muted_kinds,
        capabilities=device.capabilities,
        status=device.status,
        revoked_at=device.revoked_at,
        last_seen_at=device.last_seen_at,
        created_at=device.created_at,
        updated_at=device.updated_at,
    )


def _require_surface(device: Device) -> Device:
    if device.kind is not DeviceKind.SURFACE or device.platform not in {
        PushProvider.TELEGRAM.value,
        PushProvider.WHATSAPP.value,
    }:
        raise NotFoundError("surface not found")
    return device


class SurfaceManagementService:
    """Principal-scoped surface discovery and one-time pairing ceremony."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        code_expiry_seconds: int = 600,
        max_code_attempts: int = 5,
    ) -> None:
        if code_expiry_seconds <= 0 or max_code_attempts <= 0:
            raise ValueError("surface pairing limits must be positive")
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._code_expiry = timedelta(seconds=code_expiry_seconds)
        self._max_code_attempts = max_code_attempts

    async def list(self, principal: Principal) -> builtins.list[DeviceView]:
        require_scope(principal, "surface.read")
        async with self._uow_factory() as uow:
            rows = await uow.devices.list(principal, limit=10_000)
        return [_surface_view(row) for row in rows if row.kind is DeviceKind.SURFACE]

    async def get(self, principal: Principal, surface_id: UUID) -> DeviceView:
        require_scope(principal, "surface.read")
        async with self._uow_factory() as uow:
            surface = _require_surface(await uow.devices.get(surface_id, principal))
        return _surface_view(surface)

    async def issue_code(
        self,
        principal: Principal,
        surface_id: UUID,
        *,
        granted_scopes: frozenset[str],
        label: str | None,
        idempotency_key: str,
    ) -> IssuedPairingCode:
        require_scope(principal, "surface.write")
        normalized_key = idempotency_key.strip()
        if not normalized_key or len(normalized_key) > 255:
            raise ValueError("surface pairing idempotency key must contain 1 to 255 characters")
        if not granted_scopes <= principal.scopes:
            raise ConflictError(
                "pairing scopes exceed the current principal",
                reason="surface.scope_ceiling",
            )
        derivation = (
            f"surface.pairing.code_issued:{principal.tenant_id}:"
            f"{principal.principal_id}:{surface_id}:{normalized_key}"
        )
        now = self._clock.now()
        async with self._uow_factory() as uow:
            _require_surface(await uow.devices.get(surface_id, principal))
            if await uow.process_events.get_by_derivation(derivation) is not None:
                raise ConflictError(
                    "pairing code was already presented for this idempotency key",
                    reason="surface.pairing_code_already_issued",
                )
            issued = issue_pairing_code(
                pairing_id=self._ids.new_id(),
                surface_id=surface_id,
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                created_by_principal_id=principal.principal_id,
                granted_scopes=granted_scopes,
                label=label,
                now=now,
                expires_after=self._code_expiry,
                max_attempts=self._max_code_attempts,
            )
            await uow.surfaces.pairings.create_code(issued.record, principal)
            await uow.process_events.append(
                ProcessEvent(
                    id=self._ids.new_id(),
                    event_type="surface.pairing.code_issued",
                    actor_type="principal",
                    actor_id=principal.principal_id,
                    payload={
                        "tenant_id": principal.tenant_id,
                        "principal_id": principal.principal_id,
                        "surface_id": str(surface_id),
                        "pairing_code_id": str(issued.record.id),
                        "granted_scopes": sorted(granted_scopes),
                    },
                    derivation_key=derivation,
                    created_at=now,
                )
            )
        return issued

    async def list_pairings(self, principal: Principal, surface_id: UUID) -> builtins.list[Pairing]:
        require_scope(principal, "surface.read")
        async with self._uow_factory() as uow:
            _require_surface(await uow.devices.get(surface_id, principal))
            return await uow.surfaces.pairings.list_pairings(surface_id, principal)

    async def revoke_pairing(self, principal: Principal, pairing_id: UUID) -> Pairing:
        require_scope(principal, "surface.write")
        now = self._clock.now()
        async with self._uow_factory() as uow:
            pairing = await uow.surfaces.pairings.get_pairing(pairing_id, principal)
            if pairing.revoked_at is not None:
                return pairing
            revoked = await uow.surfaces.pairings.revoke_pairing(pairing_id, principal, now)
            await uow.surfaces.sessions.rotate_for_sender(
                pairing.surface_id,
                direct_message_key(pairing.sender_id),
                now,
            )
            surface = _require_surface(await uow.devices.get(pairing.surface_id, principal))
            if (
                surface.push_token is not None
                and surface.push_token.get_secret_value() == pairing.sender_id
            ):
                await uow.devices.upsert(surface.without_push_route(now), principal)
            await uow.process_events.append(
                ProcessEvent(
                    id=self._ids.new_id(),
                    event_type="surface.pairing.revoked",
                    actor_type="principal",
                    actor_id=principal.principal_id,
                    payload={
                        "tenant_id": principal.tenant_id,
                        "principal_id": principal.principal_id,
                        "surface_id": str(pairing.surface_id),
                        "pairing_id": str(pairing.id),
                    },
                    derivation_key=f"surface.pairing.revoked:{pairing.id}",
                    created_at=now,
                )
            )
        return revoked

    async def delete_pairing(self, principal: Principal, pairing_id: UUID) -> None:
        require_scope(principal, "surface.write")
        async with self._uow_factory() as uow:
            pairing = await uow.surfaces.pairings.get_pairing(pairing_id, principal)
            if pairing.revoked_at is None:
                raise ConflictError(
                    "pairing must be revoked before deletion",
                    reason="surface.pairing_active",
                )
            await uow.surfaces.pairings.delete_pairing(pairing_id, principal)


@dataclass(frozen=True, slots=True)
class PreparedSurfaceSubmission:
    """Post-commit work selected by the shared run-submission function."""

    run_id: UUID
    disposition: Literal[
        InboundDisposition.SUBMITTED,
        InboundDisposition.INPUT_DELIVERED,
        InboundDisposition.COMMAND_HANDLED,
    ]
    dispatch_kind: Literal["dispatch", "resume"]


@dataclass(frozen=True, slots=True)
class SurfaceIngressResult:
    """Content-free result safe for receipt replay and transport metrics."""

    disposition: InboundDisposition
    session_id: UUID | None = None
    run_id: UUID | None = None
    reason_code: str | None = None
    replayed: bool = False


def _result(receipt: InboundReceipt, *, replayed: bool = False) -> SurfaceIngressResult:
    return SurfaceIngressResult(
        disposition=receipt.disposition,
        session_id=receipt.session_id,
        run_id=receipt.run_id,
        reason_code=receipt.reason_code,
        replayed=replayed,
    )


class SurfaceIngressService:
    """Admit one authenticated transport update in one short transaction."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        principals: SchedulePrincipalDirectory,
        admission: SurfaceAdmissionController | None = None,
        max_cost_reservation: Decimal | None = None,
        clock: Clock,
        ids: IdFactory,
        create_session: SurfaceSessionCreator,
        submit: SurfaceSubmitter,
        dispatch: SurfaceDispatch,
        notice: SurfaceNotice,
        stop_run: SurfaceStopRun | None = None,
        resume_after_approval: SurfaceResumeApproval | None = None,
        notices: Mapping[PushProvider, SurfaceNotice] | None = None,
        session_idle_seconds: int = 86_400,
        max_code_attempts: int = 5,
        lockout_seconds: int = 3_600,
        per_sender_messages_per_minute: int = 20,
        inbound_text_max_chars: int = 32_768,
        self_approval_enabled: bool = True,
    ) -> None:
        if (
            min(
                session_idle_seconds,
                max_code_attempts,
                lockout_seconds,
                per_sender_messages_per_minute,
                inbound_text_max_chars,
            )
            <= 0
        ):
            raise ValueError("surface ingress limits must be positive")
        self._uow_factory = uow_factory
        self._principals = principals
        self._admission = admission
        self._max_cost_reservation = max_cost_reservation
        self._clock = clock
        self._ids = ids
        self._create_session = create_session
        self._submit = submit
        self._dispatch = dispatch
        self._notice = notice
        self._stop_run = stop_run
        self._resume_after_approval = resume_after_approval
        self._notices = dict(notices or {})
        self._session_idle = timedelta(seconds=session_idle_seconds)
        self._max_code_attempts = max_code_attempts
        self._lockout_seconds = lockout_seconds
        self._per_sender_rate = per_sender_messages_per_minute
        self._inbound_text_max = inbound_text_max_chars
        self._self_approval_enabled = self_approval_enabled
        self._sender_windows: dict[tuple[UUID, str], deque[datetime]] = {}
        self._notice_windows: dict[tuple[UUID, str, str], datetime] = {}

    async def ingest(self, update: SurfaceInboundMessage) -> SurfaceIngressResult:
        now = self._clock.now()
        prepared: PreparedSurfaceSubmission | None = None
        notice_reason: str | None = None
        async with self._uow_factory() as uow:
            placeholder = InboundReceipt(
                surface_id=update.surface_id,
                external_update_id=update.external_update_id,
                received_at=update.received_at,
                disposition=InboundDisposition.REJECTED_ADMISSION,
                reason_code="surface.processing",
            )
            if not await uow.surfaces.receipts.create(placeholder):
                existing = await uow.surfaces.receipts.get(
                    update.surface_id,
                    update.external_update_id,
                )
                if existing is None:
                    raise RuntimeError("surface receipt conflict has no stored row")
                return _result(existing, replayed=True)

            if update.chat_kind is not SurfaceChatKind.DIRECT:
                final = placeholder.model_copy(
                    update={
                        "disposition": InboundDisposition.IGNORED_CHAT_KIND,
                        "reason_code": "surface.chat_kind_unsupported",
                    }
                )
                await uow.surfaces.receipts.replace(final)
                notice_reason = final.reason_code
            elif update.message_kind is not SurfaceMessageKind.TEXT:
                final = placeholder.model_copy(
                    update={
                        "disposition": InboundDisposition.IGNORED_MEDIA,
                        "reason_code": "surface.media_unsupported",
                    }
                )
                await uow.surfaces.receipts.replace(final)
                notice_reason = final.reason_code
            elif update.text is not None and len(update.text) > self._inbound_text_max:
                final = placeholder.model_copy(
                    update={
                        "disposition": InboundDisposition.REJECTED_ADMISSION,
                        "reason_code": "surface.text_too_large",
                    }
                )
                await uow.surfaces.receipts.replace(final)
                notice_reason = final.reason_code
            elif not self._rate_allowed(update, now):
                final = placeholder.model_copy(
                    update={
                        "disposition": InboundDisposition.REJECTED_RATE,
                        "reason_code": "surface.rate_limited",
                    }
                )
                await uow.surfaces.receipts.replace(final)
                notice_reason = final.reason_code
            else:
                assert update.text is not None
                lockout = await uow.surfaces.pairings.get_lockout(
                    update.surface_id,
                    update.sender_id,
                )
                if lockout is not None and lockout.locked_until is not None:
                    if lockout.locked_until > now:
                        final = placeholder.model_copy(
                            update={
                                "disposition": InboundDisposition.REJECTED_LOCKED,
                                "reason_code": "surface.pairing_locked",
                            }
                        )
                        await uow.surfaces.receipts.replace(final)
                        notice_reason = final.reason_code
                    else:
                        await uow.surfaces.pairings.clear_lockout(
                            update.surface_id,
                            update.sender_id,
                        )
                        final, prepared, notice_reason = await self._route_text(
                            uow,
                            update,
                            placeholder,
                        )
                else:
                    final, prepared, notice_reason = await self._route_text(
                        uow,
                        update,
                        placeholder,
                    )
            if final.disposition not in {
                InboundDisposition.SUBMITTED,
                InboundDisposition.INPUT_DELIVERED,
            }:
                event_type = (
                    "surface.pairing.completed"
                    if final.reason_code == "surface.paired"
                    else "surface.pairing.locked"
                    if final.reason_code == "surface.pairing_locked"
                    else "surface.pairing.failed"
                    if final.reason_code == "surface.pairing_invalid"
                    else "surface.inbound.rejected"
                )
                await uow.process_events.append(
                    ProcessEvent(
                        id=self._ids.new_id(),
                        event_type=event_type,
                        actor_type="surface",
                        payload={
                            "surface_id": str(update.surface_id),
                            "external_update_id": update.external_update_id,
                            "disposition": final.disposition.value,
                            "reason_code": final.reason_code,
                        },
                        derivation_key=(
                            f"{event_type}:{update.surface_id}:{update.external_update_id}"
                        ),
                        created_at=now,
                    )
                )
            result = _result(final)

        if prepared is not None:
            dispatched = self._dispatch(prepared)
            if inspect.isawaitable(dispatched):
                await dispatched
        if notice_reason is not None and self._notice_allowed(update, notice_reason, now):
            notice = self._notices.get(update.provider, self._notice)
            await notice(update.chat_ref, notice_reason)
        return result

    def _rate_allowed(self, update: SurfaceInboundMessage, now: datetime) -> bool:
        key = (update.surface_id, update.sender_id)
        window = self._sender_windows.setdefault(key, deque())
        cutoff = now - timedelta(minutes=1)
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= self._per_sender_rate:
            return False
        window.append(now)
        return True

    def _notice_allowed(
        self,
        update: SurfaceInboundMessage,
        reason_code: str,
        now: datetime,
    ) -> bool:
        throttled = {
            "surface.chat_kind_unsupported",
            "surface.media_unsupported",
            "surface.pairing_invalid",
            "surface.pairing_locked",
            "surface.rate_limited",
            "surface.unpaired",
        }
        if reason_code not in throttled:
            return True
        key = (update.surface_id, update.sender_id, reason_code)
        previous = self._notice_windows.get(key)
        if previous is not None and previous > now - timedelta(minutes=1):
            return False
        self._notice_windows[key] = now
        return True

    async def _route_text(
        self,
        uow: RepositoryUnitOfWork,
        update: SurfaceInboundMessage,
        placeholder: InboundReceipt,
    ) -> tuple[InboundReceipt, PreparedSurfaceSubmission | None, str | None]:
        assert update.text is not None
        pairing = await uow.surfaces.pairings.live_pairing(
            update.surface_id,
            update.sender_id,
        )
        if pairing is None and update.text.startswith("/pair "):
            return await self._pair(uow, update, placeholder, update.text[6:].strip())
        if pairing is None:
            final = placeholder.model_copy(
                update={
                    "disposition": InboundDisposition.REJECTED_UNPAIRED,
                    "reason_code": "surface.unpaired",
                }
            )
            await uow.surfaces.receipts.replace(final)
            return final, None, final.reason_code
        if update.text == "/new":
            await uow.surfaces.sessions.rotate_for_sender(
                update.surface_id,
                direct_message_key(update.chat_ref),
                self._clock.now(),
            )
            final = placeholder.model_copy(
                update={
                    "disposition": InboundDisposition.COMMAND_HANDLED,
                    "reason_code": "surface.session_rotated",
                }
            )
            await uow.surfaces.receipts.replace(final)
            return final, None, final.reason_code
        if update.text == "/stop":
            return await self._stop(uow, update, pairing, placeholder)
        if update.text.startswith(("/approve ", "/deny ")):
            return await self._resolve_approval(uow, update, pairing, placeholder)
        if update.text in {"/help", "/status"}:
            final = placeholder.model_copy(
                update={
                    "disposition": InboundDisposition.COMMAND_HANDLED,
                    "reason_code": ("surface.help" if update.text == "/help" else "surface.status"),
                }
            )
            await uow.surfaces.receipts.replace(final)
            return final, None, final.reason_code
        return await self._submit_text(uow, update, pairing, placeholder)

    async def _bound_principal(self, pairing: Pairing) -> Principal | None:
        authority = await self._principals.current(pairing.tenant_id, pairing.principal_id)
        if authority is None or not authority.enabled:
            return None
        scopes = pairing.granted_scopes.intersection(authority.principal.scopes)
        return authority.principal.model_copy(update={"scopes": set(scopes)})

    async def _stop(
        self,
        uow: RepositoryUnitOfWork,
        update: SurfaceInboundMessage,
        pairing: Pairing,
        placeholder: InboundReceipt,
    ) -> tuple[InboundReceipt, None, str]:
        principal = await self._bound_principal(pairing)
        reason = "surface.principal_unavailable"
        if principal is not None and "run.cancel" not in principal.scopes:
            reason = "surface.scope_denied"
        elif principal is not None:
            mapping = await uow.surfaces.sessions.live(
                update.surface_id,
                direct_message_key(update.chat_ref),
            )
            active = (
                None
                if mapping is None
                else await uow.runs.active_for_session(mapping.session_id, principal)
            )
            if active is None:
                reason = "surface.no_active_run"
            else:
                if self._stop_run is None:
                    raise RuntimeError("surface stop command is not configured")
                await self._stop_run(uow, active, principal.principal_id)
                reason = "surface.run_stopped"
        final = placeholder.model_copy(
            update={
                "disposition": InboundDisposition.COMMAND_HANDLED,
                "reason_code": reason,
            }
        )
        await uow.surfaces.receipts.replace(final)
        return final, None, reason

    async def _resolve_approval(
        self,
        uow: RepositoryUnitOfWork,
        update: SurfaceInboundMessage,
        pairing: Pairing,
        placeholder: InboundReceipt,
    ) -> tuple[InboundReceipt, PreparedSurfaceSubmission | None, str]:
        assert update.text is not None
        command, candidate = update.text.split(maxsplit=1)
        principal = await self._bound_principal(pairing)
        prepared: PreparedSurfaceSubmission | None = None
        reason = "surface.principal_unavailable"
        if principal is not None and "approval.resolve" not in principal.scopes:
            reason = "surface.scope_denied"
        elif principal is not None:
            try:
                exact_id = UUID(candidate)
            except ValueError:
                exact_id = None
            if exact_id is not None:
                try:
                    exact = await uow.approvals.get(exact_id, principal)
                except NotFoundError:
                    matches = []
                else:
                    matches = [exact]
            else:
                pending = await uow.approvals.list_pending(principal, limit=100)
                matches = [row for row in pending if str(row.id).startswith(candidate)]
            if len(matches) != 1:
                reason = "surface.approval_not_found"
            elif (
                not self._self_approval_enabled
                and matches[0].principal_id == principal.principal_id
            ):
                reason = "surface.self_approval_denied"
            else:
                resolution = (
                    ApprovalResolutionType.APPROVE_ONCE
                    if command == "/approve"
                    else ApprovalResolutionType.DENY
                )
                outcome = await uow.approvals.resolve(
                    matches[0].id,
                    principal,
                    resolution,
                    "Resolved from a paired surface.",
                )
                if outcome.state is ApprovalResolutionState.ALREADY_RESOLVED_DIFFERENTLY:
                    reason = "surface.approval_already_resolved"
                else:
                    reason = "surface.approval_resolved"
                    if outcome.state is ApprovalResolutionState.APPLIED:
                        run = await uow.runs.get(outcome.approval.run_id, principal)
                        await uow.events.append(
                            NewEvent(
                                session_id=run.session_id,
                                run_id=run.id,
                                event_type="approval.resolved",
                                actor_type="surface",
                                actor_id=principal.principal_id,
                                payload={
                                    "approval_id": str(outcome.approval.id),
                                    "resolution": resolution.value,
                                },
                            )
                        )
                        if run.status is RunStatus.WAITING_FOR_APPROVAL:
                            if self._resume_after_approval is None:
                                raise RuntimeError("surface approval resume is not configured")
                            await self._resume_after_approval(uow, run)
                            prepared = PreparedSurfaceSubmission(
                                run_id=run.id,
                                disposition=InboundDisposition.COMMAND_HANDLED,
                                dispatch_kind="resume",
                            )
        final = placeholder.model_copy(
            update={
                "disposition": InboundDisposition.COMMAND_HANDLED,
                "run_id": None if prepared is None else prepared.run_id,
                "reason_code": reason,
            }
        )
        await uow.surfaces.receipts.replace(final)
        return final, prepared, reason

    async def _pair(
        self,
        uow: RepositoryUnitOfWork,
        update: SurfaceInboundMessage,
        placeholder: InboundReceipt,
        candidate: str,
    ) -> tuple[InboundReceipt, None, str]:
        codes = await uow.surfaces.pairings.active_codes_for_surface(
            update.surface_id,
            self._clock.now(),
        )
        matched = next((code for code in codes if pairing_code_matches(code, candidate)), None)
        if matched is None:
            for code in codes:
                authority = await self._principals.current(code.tenant_id, code.principal_id)
                if authority is not None:
                    await uow.surfaces.pairings.record_code_attempt(
                        code.id,
                        authority.principal,
                    )
            lockout = await uow.surfaces.pairings.record_failed_pairing(
                update.surface_id,
                update.sender_id,
                at=self._clock.now(),
                max_attempts=self._max_code_attempts,
                lockout_seconds=self._lockout_seconds,
            )
            locked = lockout.locked_until is not None
            final = placeholder.model_copy(
                update={
                    "disposition": (
                        InboundDisposition.REJECTED_LOCKED
                        if locked
                        else InboundDisposition.REJECTED_UNPAIRED
                    ),
                    "reason_code": (
                        "surface.pairing_locked" if locked else "surface.pairing_invalid"
                    ),
                }
            )
            await uow.surfaces.receipts.replace(final)
            assert final.reason_code is not None
            return final, None, final.reason_code
        authority = await self._principals.current(matched.tenant_id, matched.principal_id)
        if authority is None or not authority.enabled:
            final = placeholder.model_copy(
                update={
                    "disposition": InboundDisposition.REJECTED_ADMISSION,
                    "reason_code": "surface.principal_unavailable",
                }
            )
            await uow.surfaces.receipts.replace(final)
            assert final.reason_code is not None
            return final, None, final.reason_code
        await uow.surfaces.pairings.record_code_attempt(matched.id, authority.principal)
        await uow.surfaces.pairings.consume_code(
            matched.id,
            self._clock.now(),
            authority.principal,
        )
        await uow.surfaces.pairings.create_pairing(
            Pairing(
                id=self._ids.new_id(),
                surface_id=update.surface_id,
                tenant_id=matched.tenant_id,
                principal_id=matched.principal_id,
                sender_id=update.sender_id,
                sender_label=update.sender_label,
                granted_scopes=matched.granted_scopes,
                paired_at=self._clock.now(),
            )
        )
        surface = _require_surface(await uow.devices.get(update.surface_id, authority.principal))
        await uow.devices.upsert(
            surface.with_surface_route(update.provider, update.chat_ref, self._clock.now()),
            authority.principal,
        )
        await uow.surfaces.pairings.clear_lockout(update.surface_id, update.sender_id)
        final = placeholder.model_copy(
            update={
                "disposition": InboundDisposition.COMMAND_HANDLED,
                "reason_code": "surface.paired",
            }
        )
        await uow.surfaces.receipts.replace(final)
        return final, None, "surface.paired"

    async def _submit_text(
        self,
        uow: RepositoryUnitOfWork,
        update: SurfaceInboundMessage,
        pairing: Pairing,
        placeholder: InboundReceipt,
    ) -> tuple[InboundReceipt, PreparedSurfaceSubmission | None, str | None]:
        authority = await self._principals.current(pairing.tenant_id, pairing.principal_id)
        if authority is None or not authority.enabled:
            final = placeholder.model_copy(
                update={
                    "disposition": InboundDisposition.REJECTED_ADMISSION,
                    "reason_code": "surface.principal_unavailable",
                }
            )
            await uow.surfaces.receipts.replace(final)
            return final, None, final.reason_code
        scopes = pairing.granted_scopes.intersection(authority.principal.scopes)
        bound_principal = authority.principal.model_copy(update={"scopes": set(scopes)})
        if "run.write" not in bound_principal.scopes:
            final = placeholder.model_copy(
                update={
                    "disposition": InboundDisposition.REJECTED_ADMISSION,
                    "reason_code": "surface.scope_denied",
                }
            )
            await uow.surfaces.receipts.replace(final)
            return final, None, final.reason_code
        external_key = direct_message_key(update.chat_ref)
        mapping = await uow.surfaces.sessions.live(update.surface_id, external_key)
        session: Session | None = None
        active_status: RunStatus | None = None
        if mapping is not None:
            try:
                session = await uow.sessions.get(mapping.session_id, bound_principal)
            except NotFoundError:
                await uow.surfaces.sessions.rotate(mapping.id, self._clock.now())
                mapping = None
            else:
                latest_agent = await uow.agents.latest_version(session.agent_id)
                reason = rotation_reason(
                    mapping,
                    now=self._clock.now(),
                    idle_after=self._session_idle,
                    session_status=session.status,
                    last_message_at=mapping.last_inbound_at,
                    mapped_agent_version=session.agent_version,
                    current_agent_version=latest_agent.version,
                )
                if reason is not None:
                    await uow.surfaces.sessions.rotate(mapping.id, self._clock.now())
                    mapping = None
                    session = None
                else:
                    active = await uow.runs.active_for_session(session.id, bound_principal)
                    active_status = None if active is None else active.status
                    if active is not None and active.status is not RunStatus.WAITING_FOR_USER:
                        final = placeholder.model_copy(
                            update={
                                "disposition": InboundDisposition.REJECTED_ACTIVE_RUN,
                                "session_id": session.id,
                                "run_id": active.id,
                                "reason_code": "surface.active_run",
                            }
                        )
                        await uow.surfaces.receipts.replace(final)
                        return final, None, final.reason_code
        if active_status is None:
            admission = self._admission or uow.surfaces.admission
            decision = await admission.check(
                bound_principal.tenant_id,
                self._max_cost_reservation,
                self._clock.now(),
            )
            if not decision.allowed:
                final = placeholder.model_copy(
                    update={
                        "disposition": InboundDisposition.REJECTED_ADMISSION,
                        "session_id": None if session is None else session.id,
                        "reason_code": decision.reason_code,
                    }
                )
                await uow.surfaces.receipts.replace(final)
                return final, None, final.reason_code
        if session is None:
            session = await self._create_session(uow, bound_principal, update)
            mapping = await uow.surfaces.sessions.create(
                SurfaceSession(
                    id=self._ids.new_id(),
                    surface_id=update.surface_id,
                    tenant_id=bound_principal.tenant_id,
                    principal_id=bound_principal.principal_id,
                    external_key=external_key,
                    session_id=session.id,
                    created_at=self._clock.now(),
                )
            )
        origin: dict[str, object] = {
            "kind": update.provider.value,
            "surface_id": str(update.surface_id),
            "external_update_id": update.external_update_id,
        }
        assert update.text is not None
        assert mapping is not None
        prepared = await self._submit(
            uow,
            bound_principal,
            session,
            update.text,
            origin,
            authority.authority_version,
        )
        await uow.surfaces.sessions.touch_inbound(mapping.id, self._clock.now())
        await uow.surfaces.pairings.touch_pairing(pairing.id, self._clock.now())
        final = placeholder.model_copy(
            update={
                "disposition": prepared.disposition,
                "session_id": session.id,
                "run_id": prepared.run_id,
                "reason_code": None,
            }
        )
        await uow.surfaces.receipts.replace(final)
        return final, prepared, None
