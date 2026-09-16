"""SMS history uses accepted receipt metadata and never copies message bodies."""

from datetime import timedelta
from uuid import uuid4

import pytest

from agent_core.application.device_ingest import DEVICE_TRIAGE_INSTRUCTION, ingest_digest
from agent_core.domain.devices import DeviceIngestReceipt
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import PeopleInteraction, PeopleQuery, Person, PersonIdentifier
from agent_core.memory.formation import GovernedMemoryService
from tests.contract.people_fixtures import PeopleFields
from tests.contract.support import NOW, SESSION_ID, ids, memory_uow_factory, principal
from tests.contract.test_device_registry_contract import device


@pytest.mark.parametrize(
    "variant", ["accepted", "missing_receipt", "wrong_body", "foreign_device", "erased_source"]
)
async def test_sms_receipt_links_confirmed_sender_at_received_time_without_body(
    variant: str,
) -> None:
    clock, factory = await memory_uow_factory()
    phone = device(
        token=None,
        principal_id="foreign" if variant == "foreign_device" else principal().principal_id,
    )
    received = NOW - timedelta(days=2)
    sender, body = "+15555550123", "The garden gate needs repairing."
    digest = ingest_digest(sender, body, received)
    common: PeopleFields = {
        "tenant_id": principal().tenant_id,
        "principal_id": principal().principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    person = Person(id=uuid4(), display_name="Maya", **common)
    async with factory() as uow:
        await uow.devices.upsert(
            phone, principal().model_copy(update={"principal_id": phone.principal_id})
        )
        await uow.device_ingest.record(
            DeviceIngestReceipt(
                device_id=phone.id,
                tenant_id=principal().tenant_id,
                channel="sms",
                digest="f" * 64 if variant == "missing_receipt" else digest,
                received_at=received,
                accepted_at=NOW,
                session_id=SESSION_ID,
            )
        )
        await uow.people.put(person, expected_revision=0)
        await uow.people.put(
            PersonIdentifier(
                id=uuid4(),
                person_id=person.id,
                identifier_kind="phone",
                namespace="owner",
                value=sender,
                context="owner",
                verification="owner_confirmed",
                valid_from=received,
                **common,
            ),
            expected_revision=0,
        )
        await uow.events.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type="user.message.created",
                actor_type="device",
                actor_id=principal().principal_id,
                payload={
                    "content": DEVICE_TRIAGE_INSTRUCTION.format(
                        sender=sender,
                        body="Different source text" if variant == "wrong_body" else body,
                    ),
                    "trust": "external_untrusted",
                    "origin": {
                        "kind": "device_ingest",
                        "device_id": str(phone.id),
                        "channel": "sms",
                        "digest": digest,
                    },
                },
            )
        )
    if variant == "erased_source":
        from agent_core.memory.people_formation import source_id

        async with factory() as uow:
            events = await uow.events.list_after(SESSION_ID, 0, principal())
            from agent_core.domain.people import PeopleSource

            sid = source_id(principal(), SESSION_ID, events[-1].sequence)
            await uow.people.put(
                PeopleSource(
                    id=sid,
                    session_id=SESSION_ID,
                    event_sequence=events[-1].sequence,
                    source_kind="sms",
                    evidence_at=received,
                    source_revision=digest,
                    **common,
                ),
                expected_revision=0,
            )
            await uow.people.erase(principal(), [sid])
    service = GovernedMemoryService(factory, clock, ids(), principal(), people_enabled=True)
    await service.run(trigger="idle", scope="general", session_id=SESSION_ID)
    async with factory() as uow:
        rows = await uow.people.query(
            PeopleQuery(
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                person_id=person.id,
                kinds=["interaction"],
                sensitivity_ceiling=Sensitivity.SENSITIVE,
            )
        )
    if variant != "accepted":
        assert rows == []
        return
    assert len(rows) == 1, "admitted SMS receipt must project observed People history"
    assert isinstance(rows[0], PeopleInteraction)
    assert rows[0].occurred_at == received and rows[0].summary == "Received SMS"
    assert body not in rows[0].model_dump_json()
    await service.run(trigger="idle", scope="general", session_id=SESSION_ID, since_watermark=0)
    async with factory() as uow:
        assert (
            len(
                await uow.people.query(
                    PeopleQuery(
                        tenant_id=principal().tenant_id,
                        principal_id=principal().principal_id,
                        person_id=person.id,
                        kinds=["interaction"],
                        sensitivity_ceiling=Sensitivity.SENSITIVE,
                    )
                )
            )
            == 1
        )


@pytest.mark.parametrize(
    "variant",
    ["sent", "cancelled", "missing_invocation", "foreign_device", "wrong_run", "erased_source"],
)
async def test_outgoing_sms_history_requires_both_owned_tool_and_device_receipts(
    variant: str,
) -> None:
    from agent_core.domain.devices import DeviceInvocation, DeviceInvocationStatus
    from agent_core.domain.policies import RiskLevel, SideEffectClass
    from agent_core.domain.tools import ToolInvocation, ToolInvocationStatus, ToolSource
    from tests.contract.support import RUN_ID, run

    clock, factory = await memory_uow_factory()
    owner = principal()
    phone = device(
        token=None, principal_id="foreign" if variant == "foreign_device" else owner.principal_id
    )
    person = Person(
        id=uuid4(),
        display_name="Maya",
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        created_at=NOW,
        updated_at=NOW,
    )
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    receipt_id = uuid4()
    body = "Private source body should not be duplicated."
    async with factory() as uow:
        await uow.runs.create(run())
        await uow.devices.upsert(
            phone, owner.model_copy(update={"principal_id": phone.principal_id})
        )
        await uow.people.put(person, expected_revision=0)
        await uow.people.put(
            PersonIdentifier(
                id=uuid4(),
                person_id=person.id,
                identifier_kind="phone",
                namespace="owner",
                value="+15555550123",
                context="owner",
                verification="owner_confirmed",
                valid_from=NOW - timedelta(days=1),
                **common,
            ),
            expected_revision=0,
        )
        await uow.device_invocations.create(
            DeviceInvocation(
                id=receipt_id,
                tenant_id=owner.tenant_id,
                device_id=phone.id,
                run_id=uuid4() if variant == "wrong_run" else RUN_ID,
                tool_name="device.sms.send",
                arguments={"recipient": "+15555550123", "body": body},
                status=DeviceInvocationStatus.CANCELLED
                if variant == "cancelled"
                else DeviceInvocationStatus.SENT,
                created_at=NOW - timedelta(seconds=1),
                resolved_at=NOW,
            )
        )
        if variant != "missing_invocation":
            await uow.invocations.create(
                ToolInvocation(
                    id=uuid4(),
                    run_id=RUN_ID,
                    session_id=SESSION_ID,
                    step_number=1,
                    call_id="sms-call",
                    tool_name="device.sms.send",
                    tool_version="1.0.0",
                    tool_source=ToolSource.DEVICE,
                    side_effect=SideEffectClass.EXTERNAL_MESSAGE,
                    risk=RiskLevel.HIGH,
                    status=ToolInvocationStatus.SUCCEEDED,
                    raw_arguments="{}",
                    idempotency_key="sms-tool",
                    structured_result={"status": "sent", "invocation_id": str(receipt_id)},
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        event = await uow.events.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=RUN_ID,
                event_type="tool.call.completed",
                actor_type="runtime",
                payload={
                    "name": "device.sms.send",
                    "call_id": "sms-call",
                    "reason_code": "tool.succeeded",
                },
            )
        )
        if variant == "erased_source":
            from agent_core.domain.people import PeopleSource
            from agent_core.memory.people_formation import source_id

            sid = source_id(owner, SESSION_ID, event.sequence)
            await uow.people.put(
                PeopleSource(
                    id=sid,
                    session_id=SESSION_ID,
                    event_sequence=event.sequence,
                    source_kind="sms",
                    evidence_at=NOW,
                    source_revision="synthetic",
                    **common,
                ),
                expected_revision=0,
            )
            await uow.people.erase(owner, [sid])
    service = GovernedMemoryService(factory, clock, ids(), owner, people_enabled=True)
    await service.run(trigger="idle", scope="general", session_id=SESSION_ID)
    await service.run(trigger="idle", scope="general", session_id=SESSION_ID, since_watermark=0)
    async with factory() as uow:
        rows = await uow.people.query(
            PeopleQuery(
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                kinds=["interaction"],
                person_id=person.id,
                sensitivity_ceiling=Sensitivity.SENSITIVE,
            )
        )
        assert await uow.memories.list_memories(owner) == []
    if variant != "sent":
        assert rows == []
    else:
        assert len(rows) == 1
        assert isinstance(rows[0], PeopleInteraction)
        assert rows[0].direction == "outgoing" and rows[0].summary == "Sent SMS"
        assert rows[0].participants[0].role == "recipient" and rows[0].occurred_at == NOW
        assert body not in rows[0].model_dump_json()
