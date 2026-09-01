"""Device-channel adapters: push-wake with poll-back, and a deterministic fake.

The push-wake adapter revalidates the device's presence, writes the pending
invocation and its wake in one transaction, then polls the row until the
device answers or the invocation times out. Only the device's own result
route resolves an invocation with a device-posted status; the adapter's
timeout path expires overdue rows through the store's sweep and honors any
result that reached the row first.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.devices import (
    TERMINAL_DEVICE_INVOCATION_STATUSES,
    Device,
    DeviceInvocation,
    DeviceInvocationStatus,
    DeviceStatus,
)
from agent_core.domain.errors import DeviceChannelUnavailable, NotFoundError
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

DEVICE_INVOCATION_TIMEOUT_SECONDS = 300


class DeviceInvocationNotifier(Protocol):
    """The notification production this channel needs inside its transaction."""

    async def for_device_invocation(
        self,
        uow: RepositoryUnitOfWork,
        *,
        invocation: DeviceInvocation,
        device: Device,
    ) -> bool: ...


def _unavailable(reason: str, message: str) -> DeviceChannelUnavailable:
    return DeviceChannelUnavailable(reason, message)


class PushWakeDeviceChannel:
    """Wake one device with a content-free push and poll for its single result."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        notification_producer: DeviceInvocationNotifier,
        clock: Clock,
        invocation_timeout_seconds: int = DEVICE_INVOCATION_TIMEOUT_SECONDS,
        poll_seconds: float = 1.0,
    ) -> None:
        if invocation_timeout_seconds <= 0:
            raise ValueError("device invocation timeout must be positive")
        if poll_seconds < 0:
            raise ValueError("device invocation poll interval cannot be negative")
        self._uow_factory = uow_factory
        self._producer = notification_producer
        self._clock = clock
        self._timeout_seconds = invocation_timeout_seconds
        self._poll_seconds = poll_seconds

    async def invoke(
        self,
        *,
        device_id: UUID,
        run_id: UUID,
        invocation_id: UUID,
        tool_name: str,
        arguments: dict[str, Any],
        principal: Principal,
    ) -> DeviceInvocation:
        candidate = DeviceInvocation(
            id=invocation_id,
            tenant_id=principal.tenant_id,
            device_id=device_id,
            run_id=run_id,
            tool_name=tool_name,
            arguments=arguments,
            status=DeviceInvocationStatus.PENDING,
            created_at=self._clock.now(),
        )
        async with self._uow_factory() as uow:
            device = await self._present_device(
                uow,
                device_id=device_id,
                tool_name=tool_name,
                principal=principal,
            )
            stored = await uow.device_invocations.create(candidate)
            if stored is None:
                # A replayed invocation id already carries its own wake.
                stored = self._owned(
                    await uow.device_invocations.get(invocation_id),
                    device_id=device_id,
                    principal=principal,
                )
            else:
                await self._producer.for_device_invocation(uow, invocation=stored, device=device)
        return await self._await_result(stored, device_id=device_id, principal=principal)

    def _owned(
        self,
        invocation: DeviceInvocation,
        *,
        device_id: UUID,
        principal: Principal,
    ) -> DeviceInvocation:
        """Refuse a row the invocation id resolved to that this call does not own.

        The store resolves an invocation by id alone, so every row the adapter
        reads back rather than wrote itself is checked against the device and
        tenant this call names.
        """

        if invocation.device_id != device_id or invocation.tenant_id != principal.tenant_id:
            raise _unavailable(
                "device.invocation_not_owned",
                "the invocation identifier belongs to another device",
            )
        return invocation

    async def _present_device(
        self,
        uow: RepositoryUnitOfWork,
        *,
        device_id: UUID,
        tool_name: str,
        principal: Principal,
    ) -> Device:
        try:
            device = await uow.devices.get(device_id, principal)
        except NotFoundError as error:
            raise _unavailable(
                "device.not_found",
                "the named device is not registered to this principal",
            ) from error
        if device.status is not DeviceStatus.ACTIVE:
            raise _unavailable("device.revoked", "the named device is revoked")
        if tool_name not in device.capabilities:
            raise _unavailable(
                "device.capability_absent",
                "the named device does not grant this capability",
            )
        return device

    async def _await_result(
        self,
        invocation: DeviceInvocation,
        *,
        device_id: UUID,
        principal: Principal,
    ) -> DeviceInvocation:
        deadline = invocation.created_at + timedelta(seconds=self._timeout_seconds)
        current = invocation
        while current.status is DeviceInvocationStatus.PENDING:
            if self._clock.now() >= deadline:
                return await self._expire(
                    invocation.id,
                    device_id=device_id,
                    principal=principal,
                )
            await self._clock.sleep(self._poll_seconds)
            async with self._uow_factory() as uow:
                current = self._owned(
                    await uow.device_invocations.get(invocation.id),
                    device_id=device_id,
                    principal=principal,
                )
        return current

    async def _expire(
        self,
        invocation_id: UUID,
        *,
        device_id: UUID,
        principal: Principal,
    ) -> DeviceInvocation:
        """Sweep overdue rows, then honor whatever this invocation now holds."""

        async with self._uow_factory() as uow:
            await uow.device_invocations.expire_overdue(
                now=self._clock.now(),
                timeout_seconds=self._timeout_seconds,
            )
            return self._owned(
                await uow.device_invocations.get(invocation_id),
                device_id=device_id,
                principal=principal,
            )


class FakeDeviceChannel:
    """Deterministic device channel with scripted per-invocation results."""

    def __init__(
        self,
        *,
        clock: Clock,
        capabilities: Mapping[UUID, frozenset[str]] | None = None,
        results: Mapping[UUID, DeviceInvocationStatus] | None = None,
        default_status: DeviceInvocationStatus = DeviceInvocationStatus.SENT,
        delay_seconds: float = 0.0,
    ) -> None:
        scripted = dict(results or {})
        for status in (*scripted.values(), default_status):
            if status not in TERMINAL_DEVICE_INVOCATION_STATUSES:
                raise ValueError("scripted device results must be terminal")
        self._clock = clock
        self._capabilities = dict(capabilities or {})
        self._results = scripted
        self._default_status = default_status
        self._delay_seconds = delay_seconds
        self.invocations: list[DeviceInvocation] = []

    async def invoke(
        self,
        *,
        device_id: UUID,
        run_id: UUID,
        invocation_id: UUID,
        tool_name: str,
        arguments: dict[str, Any],
        principal: Principal,
    ) -> DeviceInvocation:
        granted = self._capabilities.get(device_id)
        if granted is None:
            raise _unavailable(
                "device.not_found",
                "the named device is not registered to this principal",
            )
        if tool_name not in granted:
            raise _unavailable(
                "device.capability_absent",
                "the named device does not grant this capability",
            )
        replayed = next(
            (value for value in self.invocations if value.id == invocation_id),
            None,
        )
        if replayed is not None:
            return replayed
        if self._delay_seconds:
            await self._clock.sleep(self._delay_seconds)
        resolved_at = self._clock.now()
        invocation = DeviceInvocation(
            id=invocation_id,
            tenant_id=principal.tenant_id,
            device_id=device_id,
            run_id=run_id,
            tool_name=tool_name,
            arguments=arguments,
            status=self._results.get(invocation_id, self._default_status),
            created_at=resolved_at,
            resolved_at=resolved_at,
        )
        self.invocations.append(invocation)
        return invocation
