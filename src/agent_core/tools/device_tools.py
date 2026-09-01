"""Device-scoped tools and the capability-derived registration that exposes them.

A device tool exists exactly while a registered, unrevoked device declares its
capability. The runtime mirrors the MCP runtime's session lifecycle: an attach
reconciles this tenant's registrations against the devices the store holds, and
the last owning session's detach withdraws them. Message recipients and bodies
travel only in the invocation row the device fetches; they appear in no result,
no event payload, and no log line.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agent_core.adapters.device_channel import DEVICE_INVOCATION_TIMEOUT_SECONDS
from agent_core.domain.agents import Principal
from agent_core.domain.devices import (
    Device,
    DeviceCapability,
    DeviceCursor,
    DeviceInvocationStatus,
    DeviceStatus,
)
from agent_core.domain.errors import DeviceChannelUnavailable, NotFoundError
from agent_core.domain.events import ProcessEvent
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import (
    IdempotencyClass,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolResult,
    ToolSource,
    ToolSpec,
)
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.ports.tools import DeviceChannel, ToolRegistry

DEVICE_SMS_SEND_TOOL_NAME = DeviceCapability.SMS_SEND.value
DEVICE_SMS_SEND_TOOL_VERSION = "1.0.0"
DEVICE_TOOL_TIMEOUT_MARGIN_SECONDS = 15
DEVICE_OFFLINE_REASON_CODE = "tool.device_offline"
DEVICE_SEND_FAILED_REASON_CODE = "tool.device_send_failed"
DEVICE_PAGE_SIZE = 50

type _RegistrationKey = tuple[str, str, str, str]


@dataclass(slots=True)
class _Registration:
    """The device one dynamic registration speaks for, and the sessions holding it."""

    device_id: UUID
    owners: set[UUID]


def _registration_key(principal: Principal) -> _RegistrationKey:
    """Key reconciliation by principal, because the device read is principal-scoped.

    A tenant-scoped key would let a co-tenant's attach — whose device read
    legitimately returns nothing — reconcile the owner's tool out of the
    registry for every session holding it.
    """

    return (
        principal.tenant_id,
        principal.principal_id,
        DEVICE_SMS_SEND_TOOL_NAME,
        DEVICE_SMS_SEND_TOOL_VERSION,
    )


_SMS_SEND_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["status"],
    "properties": {
        "status": {"type": "string", "enum": ["sent", "cancelled"]},
        "invocation_id": {"type": "string", "format": "uuid"},
    },
    "additionalProperties": False,
}


def device_sms_send_spec(
    device_id: UUID,
    *,
    invocation_timeout_seconds: int = DEVICE_INVOCATION_TIMEOUT_SECONDS,
) -> ToolSpec:
    """Declare the device send, targeted at the one device that granted it.

    The pipeline timeout sits behind the channel's own invocation timeout, so a
    silent phone resolves to the offline outcome rather than a pipeline timeout.
    """

    return ToolSpec(
        name=DEVICE_SMS_SEND_TOOL_NAME,
        version=DEVICE_SMS_SEND_TOOL_VERSION,
        description=(
            "Compose an SMS on the owner's paired iPhone; the owner's Send tap performs the send."
        ),
        input_schema={
            "type": "object",
            "required": ["recipient", "body"],
            "properties": {
                "recipient": {"type": "string", "maxLength": 64},
                "body": {"type": "string", "maxLength": 2000},
            },
            "additionalProperties": False,
        },
        output_schema=_SMS_SEND_OUTPUT_SCHEMA,
        side_effect=SideEffectClass.EXTERNAL_MESSAGE,
        risk=RiskLevel.HIGH,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        required_scopes={"device.write"},
        timeout_seconds=invocation_timeout_seconds + DEVICE_TOOL_TIMEOUT_MARGIN_SECONDS,
        maximum_output_bytes=4096,
        allow_parallel=False,
        target_kind="device",
        output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        source=ToolSource.DEVICE,
        device_id=str(device_id),
    )


def _offline() -> ToolResult:
    return ToolResult(
        ok=False,
        content=[],
        failure=ToolFailure(
            kind=ToolFailureKind.TRANSPORT,
            reason_code=DEVICE_OFFLINE_REASON_CODE,
            detail="the target device did not answer this invocation",
            retryable=False,
        ),
    )


class DeviceSmsSendTool:
    """Ask the owner's paired device to compose one text; the owner's tap sends it."""

    def __init__(
        self,
        channel: DeviceChannel,
        device_id: UUID,
        ids: IdFactory,
        *,
        invocation_timeout_seconds: int = DEVICE_INVOCATION_TIMEOUT_SECONDS,
    ) -> None:
        self._channel = channel
        self._device_id = device_id
        self._ids = ids
        self.spec = device_sms_send_spec(
            device_id,
            invocation_timeout_seconds=invocation_timeout_seconds,
        )

    async def approval_view(
        self,
        arguments: dict[str, Any],
        *,
        tenant_id: str,
    ) -> tuple[str, dict[str, Any]]:
        """Describe the action without repeating the recipient or the body."""

        del arguments, tenant_id
        return (
            "Compose one text message on the paired device; "
            "the owner's Send tap performs the send.",
            {},
        )

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        invocation_id = self._ids.new_id()
        try:
            invocation = await self._channel.invoke(
                device_id=self._device_id,
                run_id=context.run_id,
                invocation_id=invocation_id,
                tool_name=self.spec.name,
                arguments=arguments,
                principal=context.principal,
            )
        except DeviceChannelUnavailable:
            # Every unavailable reason — absent, revoked, capability withdrawn,
            # or an identifier this call does not own — is one offline outcome.
            return _offline()
        except NotFoundError:
            # The invocation row can vanish under a run-scoped cascade while the
            # adapter is still waiting on it. That is the same offline outcome.
            return _offline()
        if invocation.status is DeviceInvocationStatus.SENT:
            return ToolResult(
                ok=True,
                content=[TextPart(text="The owner sent the message from the paired device.")],
                structured={"status": "sent", "invocation_id": str(invocation.id)},
                output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
            )
        if invocation.status is DeviceInvocationStatus.CANCELLED:
            return ToolResult(
                ok=True,
                content=[TextPart(text="The owner cancelled the message on the paired device.")],
                structured={"status": "cancelled"},
                output_trust=TrustLevel.EXTERNAL_UNTRUSTED,
            )
        if invocation.status is DeviceInvocationStatus.FAILED:
            return ToolResult(
                ok=False,
                content=[],
                failure=ToolFailure(
                    kind=ToolFailureKind.UPSTREAM_ERROR,
                    reason_code=DEVICE_SEND_FAILED_REASON_CODE,
                    detail="the device reported that the message was not sent",
                    retryable=False,
                ),
            )
        return _offline()


class DeviceToolRuntime:
    """Register device tools from the capabilities live devices declare."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        registry: ToolRegistry,
        channel: DeviceChannel,
        clock: Clock,
        ids: IdFactory,
        *,
        invocation_timeout_seconds: int = DEVICE_INVOCATION_TIMEOUT_SECONDS,
        page_size: int = DEVICE_PAGE_SIZE,
    ) -> None:
        if page_size <= 0:
            raise ValueError("device page size must be positive")
        self._uow_factory = uow_factory
        self._registry = registry
        self._channel = channel
        self._clock = clock
        self._ids = ids
        self._invocation_timeout_seconds = invocation_timeout_seconds
        self._page_size = page_size
        self._session_registrations: dict[UUID, set[_RegistrationKey]] = {}
        self._registrations: dict[_RegistrationKey, _Registration] = {}
        # Concurrent sessions reconcile the same tenant-scoped key, so the read
        # and the registry mutation it decides are one critical section.
        self._lock = asyncio.Lock()

    async def prepare(self, session_id: UUID, principal: Principal) -> None:
        """Attach: reconcile this tenant's registrations against its live devices."""

        async with self._lock:
            key = _registration_key(principal)
            selected = await self._select(await self._declaring_devices(principal), principal)
            existing = self._registrations.get(key)
            if existing is not None and (selected is None or existing.device_id != selected.id):
                # The capability moved or was withdrawn; the stale registration
                # goes away for every session that held it, not only this one.
                self._withdraw(key)
            if selected is not None:
                self._attach(session_id, key, principal.tenant_id, selected)

    async def close_session(self, session_id: UUID) -> None:
        """Detach: withdraw every registration this session owned."""

        async with self._lock:
            for key in tuple(self._session_registrations.pop(session_id, ())):
                registration = self._registrations.get(key)
                if registration is None:
                    continue
                registration.owners.discard(session_id)
                if not registration.owners:
                    self._unregister(key)

    async def close(self) -> None:
        for session_id in list(self._session_registrations):
            await self.close_session(session_id)

    async def _declaring_devices(self, principal: Principal) -> list[Device]:
        """Read every active device of this principal that grants the capability."""

        declaring: list[Device] = []
        cursor: DeviceCursor | None = None
        async with self._uow_factory() as uow:
            while True:
                page = await uow.devices.list(principal, limit=self._page_size, cursor=cursor)
                declaring.extend(
                    candidate
                    for candidate in page
                    if candidate.status is DeviceStatus.ACTIVE
                    and DEVICE_SMS_SEND_TOOL_NAME in candidate.capabilities
                )
                if len(page) < self._page_size:
                    break
                last = page[-1]
                cursor = DeviceCursor(created_at=last.created_at, id=last.id)
        return sorted(declaring, key=lambda candidate: (candidate.created_at, candidate.id))

    async def _select(self, declaring: list[Device], principal: Principal) -> Device | None:
        """Choose the single declaring device; refuse a second one out loud."""

        if not declaring:
            return None
        chosen = declaring[0]
        if len(declaring) > 1:
            await self._record_conflict(chosen, declaring[1:], principal)
        return chosen

    async def _record_conflict(
        self,
        chosen: Device,
        refused: list[Device],
        principal: Principal,
    ) -> None:
        now = self._clock.now()
        refused_ids = sorted(str(candidate.id) for candidate in refused)
        async with self._uow_factory() as uow:
            await uow.process_events.append(
                ProcessEvent(
                    id=self._ids.new_id(),
                    event_type="device.tool.registration_conflict",
                    actor_type="runtime",
                    actor_id=principal.principal_id,
                    payload={
                        "tenant_id": principal.tenant_id,
                        "tool_name": DEVICE_SMS_SEND_TOOL_NAME,
                        "registered_device_id": str(chosen.id),
                        "refused_device_ids": refused_ids,
                        "event_time": now.isoformat(),
                    },
                    derivation_key=(
                        f"device.tool.registration_conflict:{principal.tenant_id}:"
                        f"{DEVICE_SMS_SEND_TOOL_NAME}:{chosen.id}:{','.join(refused_ids)}"
                    ),
                    created_at=now,
                )
            )

    def _attach(
        self,
        session_id: UUID,
        key: _RegistrationKey,
        tenant_id: str,
        device: Device,
    ) -> None:
        registration = self._registrations.get(key)
        if registration is None:
            self._registry.register_dynamic(
                DeviceSmsSendTool(
                    self._channel,
                    device.id,
                    self._ids,
                    invocation_timeout_seconds=self._invocation_timeout_seconds,
                ),
                tenant_id=tenant_id,
            )
            registration = _Registration(device_id=device.id, owners=set())
            self._registrations[key] = registration
        registration.owners.add(session_id)
        self._session_registrations.setdefault(session_id, set()).add(key)

    def _withdraw(self, key: _RegistrationKey) -> None:
        registration = self._registrations.get(key)
        if registration is None:
            return
        for owner in registration.owners:
            owned = self._session_registrations.get(owner)
            if owned is None:
                continue
            owned.discard(key)
            if not owned:
                self._session_registrations.pop(owner, None)
        self._unregister(key)

    def _unregister(self, key: _RegistrationKey) -> None:
        tenant_id, _principal_id, name, version = key
        self._registry.unregister_dynamic(name, version, tenant_id=tenant_id)
        self._registrations.pop(key, None)
