"""Milestone 20: the push-wake device channel and its expiry sweep.

Covers presence revalidation, the atomic row-and-wake transaction, the bounded
poll, the offline outcome an unanswered invocation produces, and the
maintenance sweep that expires overdue rows no adapter is still waiting on.
"""

from __future__ import annotations

import builtins
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import cast
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.device_channel import PushWakeDeviceChannel
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWork, MemoryUnitOfWorkFactory
from agent_core.bootstrap import build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.devices import DeviceInvocation, DeviceInvocationStatus
from agent_core.domain.errors import DeviceChannelUnavailable, NotFoundError
from agent_core.domain.notifications import NotificationKind, device_invocation_key
from agent_core.ports.device_channel import DeviceInvocationStore
from agent_core.runtime.worker import MaintenanceWorker
from agent_core.tools.messages import TOOL_MESSAGES
from tests.contract.support import (
    NOW,
    RUN_ID,
    SHIPPED_INVOCATION_TIMEOUT_SECONDS,
    principal,
)
from tests.contract.test_device_channel_contract import (
    DEVICE_ID,
    INVOCATION_ID,
    REPLAYED_INVOCATION_ID,
    TIMEOUT_SECONDS,
    TOOL_NAME,
    invoke,
    push_wake_channel,
    push_wake_stack,
)

_START = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
_SWEEP_INVOCATION_ID = UUID("00000000-0000-0000-0000-0000000002a0")
_FRESH_INVOCATION_ID = UUID("00000000-0000-0000-0000-0000000002a1")
_SWEEP_DEVICE_ID = UUID("00000000-0000-0000-0000-0000000002a2")
_FOREIGN_DEVICE_ID = UUID("00000000-0000-0000-0000-0000000002a3")


class _RacingInvocationStore:
    """Post the device's result at the instant the adapter starts its sweep."""

    def __init__(
        self,
        inner: DeviceInvocationStore,
        *,
        status: DeviceInvocationStatus,
        at: datetime,
    ) -> None:
        self._inner = inner
        self._status = status
        self._at = at
        self.raced = False

    async def create(self, invocation: DeviceInvocation) -> DeviceInvocation | None:
        return await self._inner.create(invocation)

    async def get(self, invocation_id: UUID) -> DeviceInvocation:
        return await self._inner.get(invocation_id)

    async def list_pending_for_device(
        self,
        device_id: UUID,
        *,
        now: datetime,
    ) -> builtins.list[DeviceInvocation]:
        return await self._inner.list_pending_for_device(device_id, now=now)

    async def record_result(
        self,
        invocation_id: UUID,
        *,
        device_id: UUID,
        status: DeviceInvocationStatus,
        at: datetime,
    ) -> DeviceInvocation:
        return await self._inner.record_result(
            invocation_id, device_id=device_id, status=status, at=at
        )

    async def expire_overdue(self, *, now: datetime, timeout_seconds: int) -> int:
        if not self.raced:
            self.raced = True
            await self._inner.record_result(
                INVOCATION_ID,
                device_id=DEVICE_ID,
                status=self._status,
                at=self._at,
            )
        return await self._inner.expire_overdue(now=now, timeout_seconds=timeout_seconds)


class _RacingUnitOfWorkFactory:
    """Wrap every unit of work so its invocation store loses the expiry race."""

    def __init__(self, inner: MemoryUnitOfWorkFactory, store: _RacingInvocationStore) -> None:
        self._inner = inner
        self._store = store

    def __call__(self) -> MemoryUnitOfWork:
        uow = self._inner()
        uow.device_invocations = self._store
        return uow

    def is_open(self) -> bool:
        return self._inner.is_open()


async def test_the_row_and_the_wake_land_in_one_transaction() -> None:
    factory, clock = await push_wake_stack()
    channel = push_wake_channel(factory, clock)

    resolved = await invoke(channel, invocation_id=INVOCATION_ID)

    async with factory() as uow:
        stored = await uow.device_invocations.get(INVOCATION_ID)
        [notification] = await uow.notification_outbox.list(principal(), limit=10)
    assert stored == resolved
    assert stored.tenant_id == principal().tenant_id
    assert stored.arguments == {"recipient": "contract-recipient", "body": "contract body"}
    assert notification.kind is NotificationKind.DEVICE_INVOCATION
    assert notification.dedupe_key == device_invocation_key(INVOCATION_ID)
    assert notification.target_device_id() == DEVICE_ID


async def test_a_device_result_posted_during_polling_is_returned() -> None:
    factory: MemoryUnitOfWorkFactory | None = None

    async def answer(sleeps: int) -> None:
        assert factory is not None
        if sleeps != 2:
            return
        async with factory() as uow:
            await uow.device_invocations.record_result(
                INVOCATION_ID,
                device_id=DEVICE_ID,
                status=DeviceInvocationStatus.SENT,
                at=NOW + timedelta(seconds=240),
            )

    factory, clock = await push_wake_stack(on_sleep=answer)
    channel = push_wake_channel(factory, clock)

    resolved = await invoke(channel, invocation_id=INVOCATION_ID)

    assert resolved.status is DeviceInvocationStatus.SENT
    assert resolved.resolved_at == NOW + timedelta(seconds=240)
    assert clock.sleeps == 2


async def test_a_silent_device_expires_and_returns_the_expired_row() -> None:
    factory, clock = await push_wake_stack()
    channel = push_wake_channel(factory, clock)

    resolved = await invoke(channel, invocation_id=INVOCATION_ID)

    assert resolved.status is DeviceInvocationStatus.EXPIRED
    assert resolved.resolved_at == NOW + timedelta(seconds=TIMEOUT_SECONDS + 60)
    assert clock.sleeps == 3


async def test_a_result_that_raced_the_expiry_sweep_is_honored() -> None:
    factory, clock = await push_wake_stack()
    async with factory() as uow:
        inner = uow.device_invocations
    store = _RacingInvocationStore(
        inner,
        status=DeviceInvocationStatus.FAILED,
        at=NOW + timedelta(seconds=TIMEOUT_SECONDS),
    )
    channel = push_wake_channel(_RacingUnitOfWorkFactory(factory, store), clock)

    resolved = await invoke(channel, invocation_id=INVOCATION_ID)

    assert store.raced
    assert resolved.status is DeviceInvocationStatus.FAILED
    assert resolved.resolved_at == NOW + timedelta(seconds=TIMEOUT_SECONDS)


def test_the_adapter_never_records_a_device_result_itself() -> None:
    source = Path(inspect.getsourcefile(PushWakeDeviceChannel) or "").read_text(encoding="utf-8")

    assert "record_result" not in source


async def test_a_revoked_device_is_refused_before_any_row_exists() -> None:
    factory, clock = await push_wake_stack()
    channel = push_wake_channel(factory, clock)
    async with factory() as uow:
        await uow.devices.revoke(DEVICE_ID, principal(), NOW)

    with pytest.raises(DeviceChannelUnavailable):
        await invoke(channel, invocation_id=INVOCATION_ID)

    async with factory() as uow:
        assert await uow.notification_outbox.list(principal(), limit=10) == []
        with pytest.raises(NotFoundError):
            await uow.device_invocations.get(INVOCATION_ID)


async def test_a_device_without_the_capability_is_refused() -> None:
    factory, clock = await push_wake_stack(capabilities=frozenset())
    channel = push_wake_channel(factory, clock)

    with pytest.raises(DeviceChannelUnavailable):
        await invoke(channel, invocation_id=INVOCATION_ID)

    async with factory() as uow:
        assert await uow.notification_outbox.list(principal(), limit=10) == []


async def test_a_replayed_invocation_wakes_the_device_exactly_once() -> None:
    factory, clock = await push_wake_stack()
    channel = push_wake_channel(factory, clock)

    first = await invoke(channel, invocation_id=REPLAYED_INVOCATION_ID)
    second = await invoke(channel, invocation_id=REPLAYED_INVOCATION_ID)

    assert second == first
    async with factory() as uow:
        rows = await uow.notification_outbox.list(principal(), limit=10)
    assert [row.dedupe_key for row in rows] == [device_invocation_key(REPLAYED_INVOCATION_ID)]


async def test_an_invocation_id_that_collides_with_a_foreign_row_is_refused() -> None:
    """The replay path resolves rows by id alone; a foreign row is never adopted."""

    factory, clock = await push_wake_stack()
    channel = push_wake_channel(factory, clock)
    foreign = DeviceInvocation(
        id=INVOCATION_ID,
        tenant_id="tenant-elsewhere",
        device_id=_FOREIGN_DEVICE_ID,
        run_id=RUN_ID,
        tool_name=TOOL_NAME,
        arguments={"recipient": "foreign-recipient", "body": "foreign body"},
        status=DeviceInvocationStatus.PENDING,
        created_at=NOW,
    )
    async with factory() as uow:
        await uow.device_invocations.create(foreign)

    with pytest.raises(DeviceChannelUnavailable):
        await invoke(channel, invocation_id=INVOCATION_ID)

    async with factory() as uow:
        assert await uow.notification_outbox.list(principal(), limit=10) == []
        assert await uow.device_invocations.get(INVOCATION_ID) == foreign


def test_the_offline_outcome_has_a_platform_authored_message() -> None:
    message = TOOL_MESSAGES["tool.device_offline"]

    assert message == ("The target device is offline or unreachable; the action was not performed.")
    assert "{" not in message and "}" not in message


def _settings(tmp_path: Path, *, device_flags: bool) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/unused",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials=MappingProxyType({}),
        interpolation=MappingProxyType({"OPENAI_MODEL": ""}),
        artifact_root=tmp_path / "artifacts",
        device_channel_enabled=device_flags,
        device_sms_enabled=device_flags,
    )


def _pending(invocation_id: UUID, *, tenant_id: str, created_at: datetime) -> DeviceInvocation:
    return DeviceInvocation(
        id=invocation_id,
        tenant_id=tenant_id,
        device_id=_SWEEP_DEVICE_ID,
        run_id=RUN_ID,
        tool_name=TOOL_NAME,
        arguments={"recipient": "sweep-recipient", "body": "sweep body"},
        status=DeviceInvocationStatus.PENDING,
        created_at=created_at,
    )


async def test_the_maintenance_pass_expires_overdue_invocations(tmp_path: Path) -> None:
    clock = FixedClock(_START)
    async with build(settings=_settings(tmp_path, device_flags=True), clock=clock) as app:
        async with app.uow_factory() as uow:
            await uow.device_invocations.create(
                _pending(
                    _SWEEP_INVOCATION_ID,
                    tenant_id=app.principal.tenant_id,
                    created_at=_START,
                )
            )
        clock.advance(timedelta(seconds=SHIPPED_INVOCATION_TIMEOUT_SECONDS + 1))
        async with app.uow_factory() as uow:
            await uow.device_invocations.create(
                _pending(
                    _FRESH_INVOCATION_ID,
                    tenant_id=app.principal.tenant_id,
                    created_at=clock.now(),
                )
            )

        maintenance = cast(MaintenanceWorker, app.maintenance_factory())
        await maintenance.run_once()

        async with app.uow_factory() as uow:
            overdue = await uow.device_invocations.get(_SWEEP_INVOCATION_ID)
            fresh = await uow.device_invocations.get(_FRESH_INVOCATION_ID)
    assert overdue.status is DeviceInvocationStatus.EXPIRED
    assert overdue.resolved_at == clock.now()
    assert fresh.status is DeviceInvocationStatus.PENDING


async def test_the_invocation_sweep_stays_unwired_while_the_device_flags_are_off(
    tmp_path: Path,
) -> None:
    clock = FixedClock(_START)
    async with build(settings=_settings(tmp_path, device_flags=False), clock=clock) as app:
        async with app.uow_factory() as uow:
            await uow.device_invocations.create(
                _pending(
                    _SWEEP_INVOCATION_ID,
                    tenant_id=app.principal.tenant_id,
                    created_at=_START,
                )
            )
        clock.advance(timedelta(seconds=SHIPPED_INVOCATION_TIMEOUT_SECONDS + 1))

        maintenance = cast(MaintenanceWorker, app.maintenance_factory())
        await maintenance.run_once()

        async with app.uow_factory() as uow:
            overdue = await uow.device_invocations.get(_SWEEP_INVOCATION_ID)
    assert overdue.status is DeviceInvocationStatus.PENDING
