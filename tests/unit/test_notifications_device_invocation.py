"""Milestone 20: DEVICE_INVOCATION as the sixth notification trigger.

Covers the closed payload vocabulary for the new kind, its dedupe key, the
producer's enqueue-once behavior, and the dispatcher's device narrowing.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.adapters.push import FakePushTransport
from agent_core.application.notification_dispatcher import NotificationDispatcher
from agent_core.application.notification_producer import NotificationProducer
from agent_core.domain.devices import DeviceInvocation, DeviceInvocationStatus, PushProvider
from agent_core.domain.notifications import (
    NOTIFICATION_TITLES,
    NotificationKind,
    NotificationPayload,
    NotificationStatus,
    device_invocation_key,
)
from tests.contract.support import NOW, memory_uow_factory, principal
from tests.contract.test_device_registry_contract import device

INVOCATION_ID = UUID("00000000-0000-0000-0000-000000009001")
DEVICE_ID = UUID("00000000-0000-0000-0000-000000009002")
OTHER_DEVICE_ID = UUID("00000000-0000-0000-0000-000000009003")
RUN_ID = UUID("00000000-0000-0000-0000-000000009004")
NOTIFICATION_ID = UUID("00000000-0000-0000-0000-000000009005")


def _invocation(**updates: object) -> DeviceInvocation:
    values: dict[str, object] = {
        "id": INVOCATION_ID,
        "tenant_id": "tenant-a",
        "device_id": DEVICE_ID,
        "run_id": RUN_ID,
        "tool_name": "device.sms.send",
        "arguments": {"recipient": "+15550000000", "body": "hello"},
        "status": DeviceInvocationStatus.PENDING,
        "created_at": NOW,
    }
    values.update(updates)
    return DeviceInvocation.model_validate(values)


def _payload(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "kind": NotificationKind.DEVICE_INVOCATION,
        "title": NOTIFICATION_TITLES[NotificationKind.DEVICE_INVOCATION],
        "status": "pending",
        "invocation_id": INVOCATION_ID,
        "device_id": DEVICE_ID,
        "notification_id": NOTIFICATION_ID,
    }
    values.update(updates)
    return values


def test_device_invocation_payload_accepts_exactly_its_identifier_set() -> None:
    payload = NotificationPayload.model_validate(_payload())

    assert payload.target_device_id() == str(DEVICE_ID)
    dumped = payload.model_dump(mode="json", exclude_none=True)
    assert set(dumped) == {
        "version",
        "kind",
        "title",
        "status",
        "invocation_id",
        "device_id",
        "notification_id",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "wrong title"),
        ("status", "sent"),
    ],
)
def test_device_invocation_payload_rejects_off_vocabulary(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        NotificationPayload.model_validate(_payload(**{field: value}))


def test_device_invocation_payload_requires_both_identifiers() -> None:
    with pytest.raises(ValidationError):
        NotificationPayload.model_validate({**_payload(), "device_id": None})
    with pytest.raises(ValidationError):
        NotificationPayload.model_validate({**_payload(), "invocation_id": None})


def test_device_invocation_payload_rejects_a_foreign_identifier() -> None:
    with pytest.raises(ValidationError):
        NotificationPayload.model_validate({**_payload(), "run_id": RUN_ID})


def test_device_invocation_dedupe_key_is_stable_and_scoped_to_invocation() -> None:
    assert device_invocation_key(INVOCATION_ID) == f"device_invocation:{INVOCATION_ID}"
    assert device_invocation_key(INVOCATION_ID) == device_invocation_key(INVOCATION_ID)
    assert device_invocation_key(INVOCATION_ID) != device_invocation_key(
        UUID("00000000-0000-0000-0000-0000000090ff")
    )


async def test_producer_enqueues_once_and_dedupes_on_replay() -> None:
    clock, factory = await memory_uow_factory()
    producer = NotificationProducer(clock=clock, ids=SequenceIdFactory())
    invocation = _invocation()
    target = device(device_id=DEVICE_ID, client_device_id="target-device")

    async with factory() as uow:
        assert await producer.for_device_invocation(uow, invocation=invocation, device=target)
        assert not await producer.for_device_invocation(uow, invocation=invocation, device=target)
        [row] = await uow.notification_outbox.list(principal(), limit=10)

    assert row.kind is NotificationKind.DEVICE_INVOCATION
    assert row.dedupe_key == device_invocation_key(INVOCATION_ID)
    assert row.payload.invocation_id == INVOCATION_ID
    assert row.payload.device_id == DEVICE_ID
    assert row.status is NotificationStatus.PENDING


async def test_producer_requires_the_invocation_own_device() -> None:
    clock, factory = await memory_uow_factory()
    producer = NotificationProducer(clock=clock, ids=SequenceIdFactory())
    invocation = _invocation()
    foreign = device(device_id=OTHER_DEVICE_ID, client_device_id="foreign-device")

    async with factory() as uow:
        with pytest.raises(ValueError, match="own device"):
            await producer.for_device_invocation(uow, invocation=invocation, device=foreign)


async def test_producer_requires_a_pending_invocation() -> None:
    clock, factory = await memory_uow_factory()
    producer = NotificationProducer(clock=clock, ids=SequenceIdFactory())
    resolved = _invocation(status=DeviceInvocationStatus.SENT, resolved_at=NOW)
    target = device(device_id=DEVICE_ID, client_device_id="target-device")

    async with factory() as uow:
        with pytest.raises(ValueError, match="pending"):
            await producer.for_device_invocation(uow, invocation=resolved, device=target)


def _dispatcher(factory, clock, ids, transport) -> NotificationDispatcher:  # type: ignore[no-untyped-def]
    return NotificationDispatcher(
        uow_factory=factory,
        transport=transport,
        providers=frozenset({PushProvider.APNS}),
        clock=clock,
        ids=ids,
        claimant="notify-a",
        batch_size=10,
        lease_seconds=30,
        retry_delays=(30, 120, 600, 3600),
    )


async def test_dispatcher_delivers_only_to_the_named_device() -> None:
    clock, factory = await memory_uow_factory()
    ids = SequenceIdFactory()
    transport = FakePushTransport()
    producer = NotificationProducer(clock=clock, ids=ids)
    invocation = _invocation()
    named = device(
        device_id=DEVICE_ID,
        client_device_id="target-device",
        token="push-token-b",  # noqa: S106
    )
    other = device(
        device_id=OTHER_DEVICE_ID,
        client_device_id="other-device",
        token="push-token-c",  # noqa: S106
    )

    async with factory() as uow:
        await uow.devices.upsert(named, principal())
        await uow.devices.upsert(other, principal())
        assert await producer.for_device_invocation(uow, invocation=invocation, device=named)

    assert await _dispatcher(factory, clock, ids, transport).run_once() == 1

    assert len(transport.calls) == 1
    delivered_target, _message = transport.calls[0]
    assert delivered_target.device_id == DEVICE_ID
