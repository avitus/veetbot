"""Shared contract for terminal-run notification producers."""

from datetime import timedelta
from uuid import UUID

from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.application.notification_producer import NotificationProducer
from agent_core.domain.notifications import NotificationKind
from agent_core.domain.runs import RunStatus
from tests.contract.support import NOW, memory_uow_factory, principal, run


async def test_notification_producer_satisfies_run_transition_contract() -> None:
    clock, factory = await memory_uow_factory()
    producer = NotificationProducer(clock=clock, ids=SequenceIdFactory())
    approval_id = UUID(int=4_701)

    async with factory() as uow:
        produced = await producer.for_run_transition(
            uow,
            run=run(status=RunStatus.RUNNING),
            principal_id=principal().principal_id,
            status=RunStatus.WAITING_FOR_APPROVAL,
            approval_id=approval_id,
            approval_expires_at=NOW + timedelta(minutes=10),
        )
        rows = await uow.notification_outbox.list(principal(), limit=10)

    assert produced is True
    assert len(rows) == 1
    assert rows[0].kind is NotificationKind.APPROVAL_REQUESTED
    assert rows[0].approval_id == approval_id
    assert rows[0].expires_at == NOW + timedelta(minutes=10)
