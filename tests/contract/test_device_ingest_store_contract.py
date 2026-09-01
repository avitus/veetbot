"""Shared device-ingest store contract."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from agent_core.adapters.persistence.device_channel import InMemoryDeviceIngestStore
from agent_core.domain.devices import DeviceIngestReceipt, DeviceTriageMapping
from agent_core.domain.errors import NotFoundError
from agent_core.ports.device_channel import DeviceIngestStore
from tests.contract.support import NOW, RUN_ID, SESSION_ID, TENANT

DEVICE_ID = UUID("00000000-0000-0000-0000-000000000270")
OTHER_DEVICE_ID = UUID("00000000-0000-0000-0000-000000000271")
ROTATED_SESSION_ID = UUID("00000000-0000-0000-0000-000000000272")

CHANNEL = "sms"
OTHER_CHANNEL = "imessage"
DIGEST_A = "a1" * 32
DIGEST_B = "b2" * 32
DIGEST_C = "c3" * 32
DIGEST_D = "d4" * 32
DIGEST_E = "e5" * 32
UNKNOWN_DIGEST = "f6" * 32

DAY = NOW.date()
LAST_INSTANT_OF_DAY = datetime(2026, 7, 25, 23, 59, 59, 999999, tzinfo=UTC)
FIRST_INSTANT_OF_NEXT_DAY = datetime(2026, 7, 26, 0, 0, 0, tzinfo=UTC)
NEXT_DAY = FIRST_INSTANT_OF_NEXT_DAY.date()


def receipt(**updates: Any) -> DeviceIngestReceipt:
    values: dict[str, Any] = {
        "device_id": DEVICE_ID,
        "tenant_id": TENANT,
        "channel": CHANNEL,
        "digest": DIGEST_A,
        "received_at": NOW,
    }
    values.update(updates)
    return DeviceIngestReceipt.model_validate(values)


async def assert_receipts_are_stored_once_and_routed_after_the_fact(
    store: DeviceIngestStore,
) -> None:
    stored = await store.record(receipt())

    assert stored == receipt()
    assert await store.record(receipt(received_at=LAST_INSTANT_OF_DAY)) is None
    assert await store.count_for_utc_day(DEVICE_ID, CHANNEL, day=DAY) == 1

    with pytest.raises(NotFoundError):
        await store.attach_routing(
            device_id=DEVICE_ID,
            channel=CHANNEL,
            digest=UNKNOWN_DIGEST,
            session_id=SESSION_ID,
            run_id=RUN_ID,
        )
    with pytest.raises(NotFoundError):
        await store.attach_routing(
            device_id=OTHER_DEVICE_ID,
            channel=CHANNEL,
            digest=DIGEST_A,
            session_id=SESSION_ID,
            run_id=RUN_ID,
        )
    await store.attach_routing(
        device_id=DEVICE_ID,
        channel=CHANNEL,
        digest=DIGEST_A,
        session_id=SESSION_ID,
        run_id=RUN_ID,
    )
    assert await store.record(receipt()) is None
    assert await store.count_for_utc_day(DEVICE_ID, CHANNEL, day=DAY) == 1


async def assert_receipt_counting_is_per_device_channel_and_utc_day(
    store: DeviceIngestStore,
) -> None:
    await store.record(receipt())
    await store.record(receipt(digest=DIGEST_B, received_at=LAST_INSTANT_OF_DAY))
    await store.record(receipt(digest=DIGEST_C, received_at=FIRST_INSTANT_OF_NEXT_DAY))
    await store.record(receipt(digest=DIGEST_D, channel=OTHER_CHANNEL))
    await store.record(receipt(digest=DIGEST_E, device_id=OTHER_DEVICE_ID))

    assert await store.count_for_utc_day(DEVICE_ID, CHANNEL, day=DAY) == 2
    assert await store.count_for_utc_day(DEVICE_ID, CHANNEL, day=NEXT_DAY) == 1
    assert await store.count_for_utc_day(DEVICE_ID, OTHER_CHANNEL, day=DAY) == 1
    assert await store.count_for_utc_day(DEVICE_ID, OTHER_CHANNEL, day=NEXT_DAY) == 0
    assert await store.count_for_utc_day(OTHER_DEVICE_ID, CHANNEL, day=DAY) == 1


async def assert_triage_mapping_is_created_then_replaced(store: DeviceIngestStore) -> None:
    assert await store.get_triage_mapping(DEVICE_ID, CHANNEL) is None

    mapping = DeviceTriageMapping(
        device_id=DEVICE_ID,
        tenant_id=TENANT,
        channel=CHANNEL,
        session_id=SESSION_ID,
    )
    await store.set_triage_mapping(mapping)

    assert await store.get_triage_mapping(DEVICE_ID, CHANNEL) == mapping
    assert await store.get_triage_mapping(DEVICE_ID, OTHER_CHANNEL) is None
    assert await store.get_triage_mapping(OTHER_DEVICE_ID, CHANNEL) is None

    rotated = mapping.model_copy(update={"session_id": ROTATED_SESSION_ID})
    await store.set_triage_mapping(rotated)

    assert await store.get_triage_mapping(DEVICE_ID, CHANNEL) == rotated


async def test_in_memory_receipts_are_stored_once_and_routed_after_the_fact() -> None:
    await assert_receipts_are_stored_once_and_routed_after_the_fact(InMemoryDeviceIngestStore())


async def test_in_memory_receipt_counting_is_per_device_channel_and_utc_day() -> None:
    await assert_receipt_counting_is_per_device_channel_and_utc_day(InMemoryDeviceIngestStore())


async def test_in_memory_triage_mapping_is_created_then_replaced() -> None:
    await assert_triage_mapping_is_created_then_replaced(InMemoryDeviceIngestStore())
