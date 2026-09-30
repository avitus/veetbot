"""Cross-device attention: acknowledgements and obsolete delivered alerts."""

from dataclasses import replace
from uuid import UUID

from agent_core.bootstrap import build
from agent_core.domain.schedules import OccurrenceDisposition
from agent_core.policy.scopes import PLATFORM_SCOPES
from tests.contract.support import principal
from tests.gates.test_notification_api_m12 import _client
from tests.integration.m2_support import memory_settings


async def test_sync_validates_scope_body_and_unknown_runs_without_partial_success() -> None:
    owner = principal().model_copy(update={"scopes": set(PLATFORM_SCOPES) | {"notification.write"}})
    settings = replace(
        memory_settings(), notification_api_enabled=True, notification_dispatch_enabled=True
    )
    async with build(settings=settings, storage="memory", principal=owner) as composition:
        async with _client(composition) as client:
            response = await client.post(
                "/v1/notifications/sync",
                json={"delivered_notification_ids": [], "seen_run_ids": []},
            )
            assert response.status_code == 200
            assert response.json() == {"obsolete_notification_ids": []}
            invalid = await client.post(
                "/v1/notifications/sync", json={"seen_run_ids": ["not-uuid"]}
            )
            assert invalid.status_code == 400
            extra = await client.post("/v1/notifications/sync", json={"clear_everything": True})
            assert extra.status_code == 400
            missing = await client.post(
                "/v1/notifications/sync", json={"seen_run_ids": [str(UUID(int=999))]}
            )
            assert missing.status_code == 404
            oversized = await client.post(
                "/v1/notifications/sync",
                json={"delivered_notification_ids": [str(UUID(int=1))] * 201},
            )
            assert oversized.status_code == 400
            from agent_core.domain.runs import RunStatus
            from tests.contract.support import agent, run, session

            async with composition.uow_factory() as uow:
                await uow.agents.put(agent())
                await uow.sessions.create(session())
                completed = run(status=RunStatus.COMPLETED)
                await uow.runs.create(completed)
            for _ in range(2):
                seen = await client.post(
                    "/v1/notifications/sync", json={"seen_run_ids": [str(completed.id)]}
                )
                assert seen.status_code == 200
            async with composition.uow_factory() as uow:
                assert await uow.notification_outbox.run_seen(owner, completed.id)
        reader = owner.model_copy(update={"scopes": {"notification.read"}})
        async with _client(composition, principal=reader) as client:
            denied = await client.post("/v1/notifications/sync", json={})
            assert denied.status_code == 403


async def test_read_receipt_precedes_enqueue_and_cleanup_preserves_pending_requests() -> None:
    from datetime import timedelta

    from agent_core.adapters.determinism import SequenceIdFactory
    from agent_core.adapters.push import FakePushTransport
    from agent_core.application.device_management import NotificationInboxService
    from agent_core.application.notification_producer import NotificationProducer
    from agent_core.domain.notifications import NotificationStatus, NotificationSyncRequest
    from agent_core.domain.runs import RunStatus
    from tests.contract.support import NOW, memory_uow_factory, run
    from tests.contract.test_approval_repository_contract import request as approval_request
    from tests.contract.test_device_registry_contract import device
    from tests.unit.test_notification_dispatcher_m12 import _dispatcher
    from tests.unit.test_notification_producer_m12 import _occurrence, _revision, _schedule

    clock, factory = await memory_uow_factory()
    ids = SequenceIdFactory()
    owner = principal().model_copy(update={"scopes": {"notification.write"}})
    service = NotificationInboxService(uow_factory=factory, clock=clock)
    completed = run(status=RunStatus.COMPLETED)
    async with factory() as uow:
        await uow.runs.create(completed)
        await uow.devices.upsert(device(), owner)
    receipt = NotificationSyncRequest(seen_run_ids=[completed.id])
    await service.sync(owner, receipt)
    await service.sync(owner, receipt)  # replay is harmless
    producer = NotificationProducer(clock=clock, ids=ids)
    approval = approval_request()
    async with factory() as uow:
        await producer.for_schedule_run_accounted(
            uow,
            schedule=_schedule(),
            occurrence=_occurrence(OccurrenceDisposition.MATERIALIZED, with_run=True),
            revision=_revision(),
            run=completed,
        )
        await uow.approvals.create(approval)
        await producer.for_run_transition(
            uow,
            run=completed,
            principal_id=owner.principal_id,
            status=RunStatus.WAITING_FOR_APPROVAL,
            approval_id=approval.id,
            approval_expires_at=approval.expires_at,
        )
        rows = await uow.notification_outbox.list(owner, limit=10)
    from agent_core.domain.notifications import NotificationKind

    result = next(row for row in rows if row.kind is NotificationKind.SCHEDULE_RUN_FINISHED)
    pending = next(row for row in rows if row.kind is NotificationKind.APPROVAL_REQUESTED)
    assert result.next_attempt_at == NOW + timedelta(seconds=30)
    response = await service.sync(
        owner,
        NotificationSyncRequest(delivered_notification_ids=[result.id, pending.id, UUID(int=999)]),
    )
    assert response.obsolete_notification_ids == [result.id]
    transport = FakePushTransport()
    dispatcher = _dispatcher(factory, clock, ids, transport)
    assert await dispatcher.run_once() == 0
    clock.advance(timedelta(seconds=30))
    assert await dispatcher.run_once() == 2
    assert len(transport.calls) == 1
    async with factory() as uow:
        [settled] = await uow.notification_outbox.get_many(owner, (result.id,))
    assert settled.status is NotificationStatus.SUPERSEDED


async def test_sync_rejects_active_and_foreign_runs_before_writing_receipts() -> None:
    import pytest

    from agent_core.application.device_management import NotificationInboxService
    from agent_core.domain.errors import ConflictError, NotFoundError
    from agent_core.domain.notifications import NotificationSyncRequest
    from agent_core.domain.runs import RunStatus
    from tests.contract.support import memory_uow_factory, run

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"notification.write"}})
    service = NotificationInboxService(uow_factory=factory, clock=clock)
    completed = run(status=RunStatus.COMPLETED)
    active = run(status=RunStatus.RUNNING).model_copy(update={"id": UUID(int=9001)})
    async with factory() as uow:
        await uow.runs.create(completed)
        await uow.runs.create(active)
    with pytest.raises(ConflictError):
        await service.sync(owner, NotificationSyncRequest(seen_run_ids=[completed.id, active.id]))
    async with factory() as uow:
        assert not await uow.notification_outbox.run_seen(owner, completed.id)
    stranger = owner.model_copy(update={"principal_id": "stranger"})
    with pytest.raises(NotFoundError):
        await service.sync(stranger, NotificationSyncRequest(seen_run_ids=[completed.id]))
