"""Dispatcher retry, staleness, concurrency, and invalidation behavior."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from uuid import UUID

import pytest
from pydantic import SecretStr

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.push import FakePushTransport
from agent_core.application.notification_dispatcher import DispatchProbe, NotificationDispatcher
from agent_core.application.notification_producer import NotificationProducer
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.devices import Device, DeviceKind, DeviceStatus, PushProvider
from agent_core.domain.notifications import (
    DeliveryOutcome,
    NewNotification,
    NotificationKind,
    NotificationPayload,
    NotificationSeverity,
    NotificationStatus,
    PushOutcome,
)
from agent_core.domain.runs import RunCheckpoint, RunStatus
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.notifications import PushTransport
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.support import NOW, memory_uow_factory, principal, run
from tests.contract.test_approval_repository_contract import request as approval_request
from tests.contract.test_device_registry_contract import DEVICE_ID, device
from tests.contract.test_notification_outbox_contract import NOTIFICATION_ID, new_notification


def _dispatcher(
    factory: UnitOfWorkFactory,
    clock: Clock,
    ids: IdFactory,
    transport: PushTransport,
    *,
    claimant: str = "notify-a",
    probe: DispatchProbe | None = None,
) -> NotificationDispatcher:
    return NotificationDispatcher(
        uow_factory=factory,
        transport=transport,
        providers=frozenset({PushProvider.APNS}),
        clock=clock,
        ids=ids,
        claimant=claimant,
        batch_size=10,
        lease_seconds=30,
        retry_delays=(30, 120, 600, 3600),
        dispatch_probe=probe,
    )


def _targeted_test_notification(
    *,
    notification_id: UUID = NOTIFICATION_ID,
    device_id: UUID = DEVICE_ID,
    key: str = "one",
) -> NewNotification:
    return new_notification(
        notification_id=notification_id,
        dedupe_key=f"device.test:{device_id}:{key}",
    )


def _broadcast_notification() -> NewNotification:
    return NewNotification(
        id=NOTIFICATION_ID,
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        kind=NotificationKind.OPS_ALERT,
        dedupe_key="ops.tenant-a.database.1",
        payload=NotificationPayload(
            kind=NotificationKind.OPS_ALERT,
            title="Production alert",
            notification_id=NOTIFICATION_ID,
            signal="database",
            severity=NotificationSeverity.WARN,
            reason_code="ops.database.unavailable",
        ),
        priority=5,
        next_attempt_at=NOW,
        created_at=NOW,
    )


async def test_malformed_test_dedupe_key_fails_closed_without_delivery() -> None:
    clock, factory = await memory_uow_factory()
    transport = FakePushTransport()
    async with factory() as uow:
        await uow.devices.upsert(device(), principal())
        assert (
            await uow.notification_outbox.enqueue(
                new_notification(dedupe_key="device.test:not-a-uuid:key")
            )
            is not None
        )

    assert await _dispatcher(factory, clock, SequenceIdFactory(), transport).run_once() == 1

    assert transport.calls == []
    async with factory() as uow:
        [notification] = await uow.notification_outbox.list(principal(), limit=10)
    assert notification.status is NotificationStatus.DISPATCHED


async def test_old_pending_notification_for_an_unconfigured_provider_is_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock, factory = await memory_uow_factory()
    assert isinstance(clock, FixedClock)
    surface = device(token=None).model_copy(
        update={
            "id": UUID(int=905),
            "client_device_id": "telegram-surface",
            "name": "Telegram surface",
            "kind": DeviceKind.SURFACE,
            "platform": "telegram",
            "push_provider": PushProvider.TELEGRAM,
            "push_token": SecretStr("paired-chat-reference"),
        }
    )
    async with factory() as uow:
        await uow.devices.upsert(surface, principal())
        assert (
            await uow.notification_outbox.enqueue(
                _targeted_test_notification(
                    device_id=UUID(int=905),
                    key="backlog-regression",
                )
            )
            is not None
        )
    clock.advance(timedelta(minutes=6))

    with caplog.at_level(logging.WARNING):
        assert (
            await _dispatcher(
                factory,
                clock,
                SequenceIdFactory(),
                FakePushTransport(),
            ).run_once()
            == 0
        )

    assert any(
        record.message == "notification pending backlog exceeded threshold"
        and getattr(record, "notification_id", None) == str(new_notification().id)
        for record in caplog.records
    )


async def test_pending_backlog_warning_has_a_per_notification_cooldown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock, factory = await memory_uow_factory()
    assert isinstance(clock, FixedClock)
    surface = device(token=None).model_copy(
        update={
            "id": UUID(int=906),
            "client_device_id": "telegram-cooldown-surface",
            "name": "Telegram cooldown surface",
            "kind": DeviceKind.SURFACE,
            "platform": "telegram",
            "push_provider": PushProvider.TELEGRAM,
            "push_token": SecretStr("paired-chat-cooldown-reference"),
        }
    )
    async with factory() as uow:
        await uow.devices.upsert(surface, principal())
        assert (
            await uow.notification_outbox.enqueue(
                _targeted_test_notification(
                    device_id=surface.id,
                    key="backlog-cooldown-regression",
                )
            )
            is not None
        )
    clock.advance(timedelta(minutes=6))
    dispatcher = _dispatcher(
        factory,
        clock,
        SequenceIdFactory(),
        FakePushTransport(),
    )

    with caplog.at_level(logging.WARNING):
        assert await dispatcher.run_once() == 0
        assert await dispatcher.run_once() == 0

    warnings = [
        record
        for record in caplog.records
        if record.message == "notification pending backlog exceeded threshold"
        and getattr(record, "notification_id", None) == str(new_notification().id)
    ]
    assert len(warnings) == 1

    clock.advance(timedelta(minutes=5))
    with caplog.at_level(logging.WARNING):
        assert await dispatcher.run_once() == 0
    warnings = [
        record
        for record in caplog.records
        if record.message == "notification pending backlog exceeded threshold"
        and getattr(record, "notification_id", None) == str(new_notification().id)
    ]
    assert len(warnings) == 2


async def test_two_dispatchers_deliver_one_target_once_and_record_the_attempt() -> None:
    clock, factory = await memory_uow_factory()
    ids = SequenceIdFactory()
    transport = FakePushTransport()
    async with factory() as uow:
        await uow.devices.upsert(device(), principal())
        assert await uow.notification_outbox.enqueue(_targeted_test_notification()) is not None

    results = await asyncio.gather(
        _dispatcher(factory, clock, ids, transport).run_once(),
        _dispatcher(factory, clock, ids, transport, claimant="notify-b").run_once(),
    )

    assert sum(results) == 1
    assert len(transport.calls) == 1
    async with factory() as uow:
        [notification] = await uow.notification_outbox.list(principal(), limit=10)
        assert notification.status is NotificationStatus.DISPATCHED
        [delivery] = await uow.notification_outbox.list_deliveries(notification.id)
        assert delivery.outcome is DeliveryOutcome.DELIVERED
        assert delivery.attempt == 1


async def test_expired_claim_cannot_overwrite_newer_successful_settlement() -> None:
    clock, factory = await memory_uow_factory()
    assert isinstance(clock, FixedClock)
    ids = SequenceIdFactory()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    release_second = asyncio.Event()

    class SlowRetryTransport:
        async def deliver(self, target, message):  # type: ignore[no-untyped-def]
            del target, message
            first_started.set()
            await release_first.wait()
            return PushOutcome(
                outcome=DeliveryOutcome.RETRY,
                provider_reason="ServiceUnavailable",
            )

    class SlowSuccessTransport:
        async def deliver(self, target, message):  # type: ignore[no-untyped-def]
            del target, message
            second_started.set()
            await release_second.wait()
            return PushOutcome(outcome=DeliveryOutcome.DELIVERED)

    async with factory() as uow:
        await uow.devices.upsert(device(), principal())
        assert await uow.notification_outbox.enqueue(_targeted_test_notification()) is not None

    stale_dispatch = asyncio.create_task(
        _dispatcher(factory, clock, ids, SlowRetryTransport()).run_once()
    )
    await first_started.wait()
    clock.advance(timedelta(seconds=31))
    current_dispatch = asyncio.create_task(
        _dispatcher(
            factory,
            clock,
            ids,
            SlowSuccessTransport(),
            claimant="notify-b",
        ).run_once()
    )
    await second_started.wait()
    release_first.set()
    assert await stale_dispatch == 1

    async with factory() as uow:
        [notification] = await uow.notification_outbox.list(principal(), limit=10)
    assert notification.status is NotificationStatus.PENDING
    assert notification.attempts == 2
    assert notification.claimed_by == "notify-b"

    release_second.set()
    assert await current_dispatch == 1
    async with factory() as uow:
        [notification] = await uow.notification_outbox.list(principal(), limit=10)
        deliveries = await uow.notification_outbox.list_deliveries(notification.id)
    assert notification.status is NotificationStatus.DISPATCHED
    assert notification.attempts == 2
    assert {(delivery.attempt, delivery.outcome) for delivery in deliveries} == {
        (1, DeliveryOutcome.RETRY),
        (2, DeliveryOutcome.DELIVERED),
    }


async def test_unexpected_failure_isolated_to_one_claimed_notification() -> None:
    clock, factory = await memory_uow_factory()
    ids = SequenceIdFactory()

    class FailFirstTransport:
        def __init__(self) -> None:
            self.calls = 0

        async def deliver(self, target, message):  # type: ignore[no-untyped-def]
            del target, message
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("unexpected provider failure")
            return PushOutcome(outcome=DeliveryOutcome.DELIVERED)

    transport = FailFirstTransport()
    async with factory() as uow:
        await uow.devices.upsert(device(), principal())
        assert await uow.notification_outbox.enqueue(_targeted_test_notification()) is not None
        assert (
            await uow.notification_outbox.enqueue(
                _targeted_test_notification(
                    notification_id=UUID(int=902),
                    key="second-after-failure",
                )
            )
            is not None
        )

    assert await _dispatcher(factory, clock, ids, transport).run_once() == 2
    assert transport.calls == 2
    async with factory() as uow:
        rows = await uow.notification_outbox.list(principal(), limit=10)
        assert next(
            row for row in rows if row.dedupe_key.endswith(":second-after-failure")
        ).status is (NotificationStatus.DISPATCHED)


async def test_transport_failure_records_prior_target_and_retries_failed_target() -> None:
    clock, factory = await memory_uow_factory()
    ids = SequenceIdFactory()

    class FailSecondTransport:
        def __init__(self) -> None:
            self.calls = 0

        async def deliver(self, target, message):  # type: ignore[no-untyped-def]
            del target, message
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("sensitive provider detail")
            return PushOutcome(outcome=DeliveryOutcome.DELIVERED, provider_id="accepted")

    transport = FailSecondTransport()
    async with factory() as uow:
        await uow.devices.upsert(device(), principal())
        await uow.devices.upsert(
            device(
                device_id=UUID(int=903),
                client_device_id="client-device-b",
                token="push-token-b",
            ),
            principal(),
        )
        assert await uow.notification_outbox.enqueue(_broadcast_notification()) is not None

    assert await _dispatcher(factory, clock, ids, transport).run_once() == 1
    assert transport.calls == 2
    async with factory() as uow:
        [notification] = await uow.notification_outbox.list(principal(), limit=10)
        deliveries = await uow.notification_outbox.list_deliveries(notification.id)
        assert notification.status is NotificationStatus.PENDING
        assert [delivery.outcome for delivery in deliveries] == [
            DeliveryOutcome.DELIVERED,
            DeliveryOutcome.RETRY,
        ]
        assert deliveries[1].provider_reason == "TransportError"
        assert "sensitive" not in repr(deliveries[1])


async def test_claim_is_partitioned_by_pending_target_provider() -> None:
    clock, factory = await memory_uow_factory()
    ids = SequenceIdFactory()
    transport = FakePushTransport()
    surface = Device(
        id=UUID(int=905),
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        client_device_id="telegram-surface",
        name="Telegram surface",
        kind=DeviceKind.SURFACE,
        platform="telegram",
        push_provider=PushProvider.TELEGRAM,
        push_token=SecretStr("paired-chat-reference"),
        muted_kinds=frozenset(),
        status=DeviceStatus.ACTIVE,
        last_seen_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    async with factory() as uow:
        await uow.devices.upsert(surface, principal())
        assert (
            await uow.notification_outbox.enqueue(_targeted_test_notification(device_id=surface.id))
            is not None
        )

    assert await _dispatcher(factory, clock, ids, transport).run_once() == 0
    telegram = NotificationDispatcher(
        uow_factory=factory,
        transport=transport,
        providers=frozenset({PushProvider.TELEGRAM}),
        clock=clock,
        ids=ids,
        claimant="surface-a",
    )
    assert await telegram.run_once() == 1
    assert len(transport.calls) == 1
    assert transport.calls[0][0].provider is PushProvider.TELEGRAM


async def test_retry_schedule_is_bounded_and_expired_rows_are_never_sent() -> None:
    clock, factory = await memory_uow_factory()
    assert isinstance(clock, FixedClock)
    ids = SequenceIdFactory()
    transport = FakePushTransport(
        PushOutcome(outcome=DeliveryOutcome.RETRY, provider_reason="ServiceUnavailable")
        for _ in range(5)
    )
    async with factory() as uow:
        await uow.devices.upsert(device(), principal())
        assert await uow.notification_outbox.enqueue(_targeted_test_notification()) is not None
        expired = _targeted_test_notification(
            notification_id=UUID(int=901),
            key="expired",
        ).model_copy(update={"expires_at": NOW})
        assert await uow.notification_outbox.enqueue(expired) is not None
    clock.advance(timedelta(seconds=1))

    dispatcher = _dispatcher(factory, clock, ids, transport)
    for delay in (30, 120, 600, 3600):
        assert await dispatcher.run_once() >= 1
        clock.advance(timedelta(seconds=delay))
    assert await dispatcher.run_once() == 1

    async with factory() as uow:
        rows = await uow.notification_outbox.list(principal(), limit=10)
        retried = next(row for row in rows if row.dedupe_key.endswith(":one"))
        expired_row = next(row for row in rows if row.dedupe_key.endswith(":expired"))
        assert retried.status is NotificationStatus.FAILED
        assert retried.attempts == 5
        assert expired_row.status is NotificationStatus.EXPIRED
        assert len(await uow.notification_outbox.list_deliveries(retried.id)) == 5
        assert await uow.notification_outbox.list_deliveries(expired_row.id) == []
    assert len(transport.calls) == 5


async def _assert_deleted_session_is_superseded_without_transport_call() -> None:
    clock, factory = await memory_uow_factory()
    ids = SequenceIdFactory()
    producer = NotificationProducer(clock=clock, ids=ids)
    transport = FakePushTransport()
    missing = run(status=RunStatus.FAILED).model_copy(
        update={"id": UUID(int=910), "session_id": UUID(int=911)}
    )
    async with factory() as uow:
        await uow.devices.upsert(device(), principal())
        assert await producer.for_run_transition(
            uow,
            run=missing,
            principal_id=principal().principal_id,
            status=RunStatus.FAILED,
        )

    assert await _dispatcher(factory, clock, ids, transport).run_once() == 1
    assert transport.calls == []
    async with factory() as uow:
        [notification] = await uow.notification_outbox.list(principal(), limit=10)
        assert notification.status is NotificationStatus.SUPERSEDED


async def _assert_resolved_or_expired_approval_is_superseded_without_send() -> None:
    for expired in (False, True):
        clock, factory = await memory_uow_factory()
        ids = SequenceIdFactory()
        producer = NotificationProducer(clock=clock, ids=ids)
        transport = FakePushTransport()
        approval = approval_request().model_copy(
            update={"expires_at": NOW if expired else NOW + timedelta(minutes=5)}
        )
        async with factory() as uow:
            await uow.devices.upsert(device(), principal())
            await uow.approvals.create(approval)
            assert await producer.for_run_transition(
                uow,
                run=run(status=RunStatus.WAITING_FOR_APPROVAL),
                principal_id=principal().principal_id,
                status=RunStatus.WAITING_FOR_APPROVAL,
                approval_id=approval.id,
                approval_expires_at=approval.expires_at,
            )
            if not expired:
                await uow.approvals.resolve(
                    approval.id,
                    principal(),
                    ApprovalResolutionType.DENY,
                    None,
                )

        assert await _dispatcher(factory, clock, ids, transport).run_once() == 1
        async with factory() as uow:
            [notification] = await uow.notification_outbox.list(principal(), limit=10)
            assert notification.status is NotificationStatus.SUPERSEDED
        assert transport.calls == []


async def _assert_answered_question_or_run_no_longer_waiting_is_superseded() -> None:
    for answered in (False, True):
        clock, factory = await memory_uow_factory()
        ids = SequenceIdFactory()
        producer = NotificationProducer(clock=clock, ids=ids)
        transport = FakePushTransport()
        question_id = UUID(int=920)
        stored_run = run(status=RunStatus.WAITING_FOR_USER if answered else RunStatus.COMPLETED)
        async with factory() as uow:
            await uow.devices.upsert(device(), principal())
            await uow.runs.create(stored_run)
            if answered:
                await uow.checkpoints.write(
                    stored_run.id,
                    RunCheckpoint(
                        run_id=stored_run.id,
                        version=1,
                        status=stored_run.status,
                        working_state={"outstanding_question_id": str(UUID(int=921))},
                        created_at=NOW,
                    ),
                    full=True,
                )
            assert await producer.for_run_transition(
                uow,
                run=stored_run,
                principal_id=principal().principal_id,
                status=RunStatus.WAITING_FOR_USER,
                question_id=question_id,
            )

        assert await _dispatcher(factory, clock, ids, transport).run_once() == 1
        async with factory() as uow:
            [notification] = await uow.notification_outbox.list(principal(), limit=10)
            assert notification.status is NotificationStatus.SUPERSEDED
        assert transport.calls == []


async def test_stale_notification_catalog_is_suppressed_without_transport() -> None:
    await _assert_deleted_session_is_superseded_without_transport_call()
    await _assert_resolved_or_expired_approval_is_superseded_without_send()
    await _assert_answered_question_or_run_no_longer_waiting_is_superseded()


async def test_unregistered_tokens_invalidate_once_but_transient_failures_do_not() -> None:
    for reason in ("Unregistered", "BadDeviceToken"):
        clock, factory = await memory_uow_factory()
        ids = SequenceIdFactory()
        transport = FakePushTransport(
            [PushOutcome(outcome=DeliveryOutcome.UNREGISTERED, provider_reason=reason)]
        )
        registered = device()
        async with factory() as uow:
            await uow.devices.upsert(registered, principal())
            assert await uow.notification_outbox.enqueue(_targeted_test_notification()) is not None

        dispatcher = _dispatcher(factory, clock, ids, transport)
        assert await dispatcher.run_once() == 1
        assert await dispatcher.run_once() == 0
        async with factory() as uow:
            invalidated = await uow.devices.get(registered.id, principal())
            assert invalidated.push_token is None
            [event] = await uow.process_events.list("device.push_token_invalidated")
            assert event.payload["token_fingerprint"] == "e94244"
            assert "push-token-a" not in event.model_dump_json()
            assert (
                await uow.devices.push_targets(
                    principal().tenant_id,
                    principal().principal_id,
                    new_notification().kind,
                )
                == []
            )

    for reason in ("ServiceUnavailable", "TooManyRequests"):
        clock, factory = await memory_uow_factory()
        ids = SequenceIdFactory()
        transport = FakePushTransport(
            [PushOutcome(outcome=DeliveryOutcome.RETRY, provider_reason=reason)]
        )
        registered = device()
        async with factory() as uow:
            await uow.devices.upsert(registered, principal())
            assert await uow.notification_outbox.enqueue(_targeted_test_notification()) is not None

        assert await _dispatcher(factory, clock, ids, transport).run_once() == 1
        async with factory() as uow:
            retained = await uow.devices.get(registered.id, principal())
            assert retained.push_token is not None
            assert await uow.process_events.list("device.push_token_invalidated") == []


async def test_crash_after_transport_accept_replays_with_same_collapse_key() -> None:
    clock, factory = await memory_uow_factory()
    assert isinstance(clock, FixedClock)
    ids = SequenceIdFactory()
    transport = FakePushTransport()
    accepted = False

    def crash_once(boundary: str) -> None:
        nonlocal accepted
        assert boundary == "transport_accepted"
        if not accepted:
            accepted = True
            raise RuntimeError("injected crash after transport accept")

    async with factory() as uow:
        await uow.devices.upsert(device(), principal())
        assert await uow.notification_outbox.enqueue(_targeted_test_notification()) is not None

    with pytest.raises(RuntimeError, match="injected crash"):
        await _dispatcher(factory, clock, ids, transport, probe=crash_once).run_once()
    assert len(transport.calls) == 1

    clock.advance(timedelta(seconds=31))
    assert await _dispatcher(factory, clock, ids, transport, claimant="notify-b").run_once() == 1

    assert len(transport.calls) == 2
    assert transport.calls[0][1].dedupe_key == transport.calls[1][1].dedupe_key
    assert transport.calls[0][1].dedupe_key.endswith(":one")
    async with factory() as uow:
        [notification] = await uow.notification_outbox.list(principal(), limit=10)
        assert notification.status is NotificationStatus.DISPATCHED
        assert notification.attempts == 2
        [delivery] = await uow.notification_outbox.list_deliveries(notification.id)
        assert delivery.attempt == 2
