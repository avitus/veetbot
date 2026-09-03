"""Shared device-channel contract exercised by every DeviceChannel adapter.

Every adapter returns a terminal invocation, refuses a device that is not
present, and treats a replayed invocation id as the same logical call. The
suite injects a stepping clock and ``poll_seconds=0`` so no test sleeps.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.device_channel import FakeDeviceChannel, PushWakeDeviceChannel
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.application.notification_producer import NotificationProducer
from agent_core.domain.agents import Principal
from agent_core.domain.devices import (
    TERMINAL_DEVICE_INVOCATION_STATUSES,
    DeviceCapability,
    DeviceInvocation,
)
from agent_core.domain.errors import DeviceChannelUnavailable
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.ports.tools import DeviceChannel
from tests.contract.support import NOW, RUN_ID, memory_uow_factory, principal
from tests.contract.test_device_registry_contract import device

DEVICE_ID = UUID("00000000-0000-0000-0000-000000000280")
ABSENT_DEVICE_ID = UUID("00000000-0000-0000-0000-000000000281")
INVOCATION_ID = UUID("00000000-0000-0000-0000-000000000282")
REPLAYED_INVOCATION_ID = UUID("00000000-0000-0000-0000-000000000283")
TOOL_NAME = DeviceCapability.SMS_SEND.value
ARGUMENTS: dict[str, object] = {"recipient": "contract-recipient", "body": "contract body"}
TIMEOUT_SECONDS = 300


class SteppingClock:
    """Deterministic clock that advances one fixed step per awaited sleep."""

    def __init__(
        self,
        current: datetime,
        *,
        step: timedelta,
        on_sleep: Callable[[int], Awaitable[None]] | None = None,
    ) -> None:
        self._current = current
        self._step = step
        self._on_sleep = on_sleep
        self.sleeps = 0

    def now(self) -> datetime:
        return self._current

    async def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self._current += self._step
        if self._on_sleep is not None:
            await self._on_sleep(self.sleeps)


async def invoke(
    channel: DeviceChannel,
    *,
    invocation_id: UUID,
    device_id: UUID = DEVICE_ID,
    supplied_principal: Principal | None = None,
) -> DeviceInvocation:
    return await channel.invoke(
        device_id=device_id,
        run_id=RUN_ID,
        invocation_id=invocation_id,
        tool_name=TOOL_NAME,
        arguments=dict(ARGUMENTS),
        principal=supplied_principal or principal(),
    )


async def assert_invocation_resolves_terminally(
    channel: DeviceChannel,
    *,
    invocation_id: UUID = INVOCATION_ID,
) -> DeviceInvocation:
    """Every adapter answers with one terminal row describing the same call."""

    resolved = await invoke(channel, invocation_id=invocation_id)

    assert resolved.id == invocation_id
    assert resolved.device_id == DEVICE_ID
    assert resolved.run_id == RUN_ID
    assert resolved.tool_name == TOOL_NAME
    assert resolved.status in TERMINAL_DEVICE_INVOCATION_STATUSES
    assert resolved.resolved_at is not None
    return resolved


async def assert_an_absent_device_is_refused(channel: DeviceChannel) -> None:
    """Presence is revalidated before any row or wake exists."""

    with pytest.raises(DeviceChannelUnavailable):
        await invoke(channel, invocation_id=INVOCATION_ID, device_id=ABSENT_DEVICE_ID)


async def assert_a_foreign_principal_is_refused(channel: DeviceChannel) -> None:
    """A real device identifier never grants another principal its capability."""

    foreign = principal().model_copy(update={"principal_id": "principal-elsewhere"}, deep=True)
    with pytest.raises(DeviceChannelUnavailable):
        await invoke(
            channel,
            invocation_id=INVOCATION_ID,
            supplied_principal=foreign,
        )


async def assert_a_replayed_invocation_id_is_idempotent(
    channel: DeviceChannel,
) -> DeviceInvocation:
    """A replayed invocation id resolves to the row the first call produced."""

    first = await invoke(channel, invocation_id=REPLAYED_INVOCATION_ID)
    second = await invoke(channel, invocation_id=REPLAYED_INVOCATION_ID)

    assert second == first
    return first


async def push_wake_stack(
    *,
    step_seconds: int = 120,
    on_sleep: Callable[[int], Awaitable[None]] | None = None,
    capabilities: frozenset[str] = frozenset({TOOL_NAME}),
) -> tuple[MemoryUnitOfWorkFactory, SteppingClock]:
    """Seed one capable device and return its unit of work plus a stepping clock."""

    _fixed, factory = await memory_uow_factory()
    clock = SteppingClock(NOW, step=timedelta(seconds=step_seconds), on_sleep=on_sleep)
    async with factory() as uow:
        await uow.devices.upsert(
            device(device_id=DEVICE_ID, capabilities=capabilities),
            principal(),
        )
    return factory, clock


def push_wake_channel(
    factory: UnitOfWorkFactory,
    clock: Clock,
    *,
    timeout_seconds: int = TIMEOUT_SECONDS,
) -> PushWakeDeviceChannel:
    return PushWakeDeviceChannel(
        uow_factory=factory,
        notification_producer=NotificationProducer(clock=clock, ids=SequenceIdFactory()),
        clock=clock,
        invocation_timeout_seconds=timeout_seconds,
        poll_seconds=0,
    )


def fake_channel() -> FakeDeviceChannel:
    return FakeDeviceChannel(
        clock=FixedClock(NOW),
        capabilities={DEVICE_ID: frozenset({TOOL_NAME})},
        owners={DEVICE_ID: principal()},
    )


async def test_push_wake_channel_resolves_terminally() -> None:
    factory, clock = await push_wake_stack()

    await assert_invocation_resolves_terminally(push_wake_channel(factory, clock))


async def test_push_wake_channel_refuses_an_absent_device() -> None:
    factory, clock = await push_wake_stack()

    await assert_an_absent_device_is_refused(push_wake_channel(factory, clock))


async def test_push_wake_channel_refuses_a_foreign_principal() -> None:
    factory, clock = await push_wake_stack()

    await assert_a_foreign_principal_is_refused(push_wake_channel(factory, clock))


async def test_push_wake_channel_is_idempotent_on_a_replayed_invocation_id() -> None:
    factory, clock = await push_wake_stack()

    await assert_a_replayed_invocation_id_is_idempotent(push_wake_channel(factory, clock))


async def test_fake_channel_resolves_terminally() -> None:
    await assert_invocation_resolves_terminally(fake_channel())


async def test_fake_channel_refuses_an_absent_device() -> None:
    await assert_an_absent_device_is_refused(fake_channel())


async def test_fake_channel_refuses_a_foreign_principal() -> None:
    await assert_a_foreign_principal_is_refused(fake_channel())


async def test_fake_channel_is_idempotent_on_a_replayed_invocation_id() -> None:
    channel = fake_channel()

    await assert_a_replayed_invocation_id_is_idempotent(channel)

    assert len(channel.invocations) == 1
