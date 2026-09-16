"""Project accepted SMS receipt metadata without retaining another copy of its body."""

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid5

from agent_core.domain.agents import Principal
from agent_core.domain.errors import NotFoundError
from agent_core.domain.events import EventEnvelope
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import InteractionParticipant, PeopleInteraction, PeopleSource
from agent_core.memory.communication_sources import _sms_parts
from agent_core.memory.people import resolve_identity
from agent_core.memory.people_formation import _Common, source_id
from agent_core.ports.persistence import RepositoryUnitOfWork


async def project_sms(
    uow: RepositoryUnitOfWork, principal: Principal, event: EventEnvelope, now: datetime
) -> bool:
    """The caller holds the owner lock; receipt and device ownership are rechecked here."""
    parts = _sms_parts(event, principal)
    origin = event.payload.get("origin")
    if parts is None or not isinstance(origin, dict):
        return False
    try:
        device_id = UUID(str(origin["device_id"]))
        digest = str(origin["digest"])
        await uow.devices.get(device_id, principal)
    except (ValueError, KeyError, NotFoundError):
        return False
    receipt = await uow.device_ingest.get(device_id, "sms", digest)
    if (
        receipt is None
        or receipt.tenant_id != principal.tenant_id
        or receipt.session_id != event.session_id
    ):
        return False
    sender, body = parts
    instant = receipt.received_at.astimezone(UTC).isoformat()
    # Verify the existing device-ingest wire digest; no body is written below.
    if hashlib.sha256(f"{sender}\n{body}\n{instant}".encode()).hexdigest() != digest:
        return False
    try:
        match = await resolve_identity(
            uow.people,
            principal,
            kind="phone",
            namespace="owner",
            value=sender,
            context="owner",
            at=receipt.received_at,
            ceiling=Sensitivity.SENSITIVE,
        )
    except ValueError:
        return False
    if match.status != "matched":
        return False
    sid = source_id(principal, event.session_id, event.sequence)
    interaction_id = uuid5(device_id, "people-sms@1:" + digest)
    if await uow.people.source_suppressed(principal, sid) or await uow.people.is_erased(
        principal, interaction_id
    ):
        return False
    if await uow.people.get(principal, interaction_id, ceiling=Sensitivity.RESTRICTED):
        return False
    common: _Common = {
        "tenant_id": principal.tenant_id,
        "principal_id": principal.principal_id,
        "created_at": now,
        "updated_at": now,
        "sensitivity": Sensitivity.SENSITIVE,
    }
    if await uow.people.get(principal, sid, ceiling=Sensitivity.RESTRICTED) is None:
        await uow.people.put(
            PeopleSource(
                id=sid,
                session_id=event.session_id,
                event_sequence=event.sequence,
                source_kind="sms",
                evidence_at=receipt.received_at,
                source_revision=digest,
                **common,
            ),
            expected_revision=0,
        )
    await uow.people.put(
        PeopleInteraction(
            id=interaction_id,
            channel="sms",
            interaction_kind="exchange",
            attribution="observed",
            direction="incoming",
            summary="Received SMS",
            occurred_at=receipt.received_at,
            precision="instant",
            participants=[
                InteractionParticipant(
                    person_id=match.person_ids[0],
                    role="sender",
                )
            ],
            support_ids=[sid],
            **common,
        ),
        expected_revision=0,
    )
    return True


async def project_sent_sms(
    uow: RepositoryUnitOfWork, principal: Principal, event: EventEnvelope, now: datetime
) -> bool:
    """Only a persisted successful tool result and owned SENT receipt prove a send."""
    from agent_core.domain.devices import DeviceInvocationStatus
    from agent_core.domain.tools import ToolInvocationStatus, ToolSource

    if (
        event.event_type != "tool.call.completed"
        or event.actor_type != "runtime"
        or event.payload.get("name") != "device.sms.send"
        or event.run_id is None
        or not isinstance(event.payload.get("call_id"), str)
    ):
        return False
    try:
        run = await uow.runs.get(event.run_id, principal)
        if run.session_id != event.session_id:
            return False
        calls = await uow.invocations.list_for_run(run.id, principal)
        matches = [
            call
            for call in calls
            if call.call_id == event.payload["call_id"]
            and call.session_id == event.session_id
            and call.tool_name == "device.sms.send"
            and call.tool_source == ToolSource.DEVICE
            and call.status == ToolInvocationStatus.SUCCEEDED
        ]
        if len(matches) != 1:
            return False
        result = matches[0].structured_result
        if result is None or result.get("status") != "sent":
            return False
        receipt = await uow.device_invocations.get(UUID(str(result.get("invocation_id"))))
        await uow.devices.get(receipt.device_id, principal)
    except (ValueError, NotFoundError):
        return False
    recipient = receipt.arguments.get("recipient")
    if (
        receipt.tenant_id != principal.tenant_id
        or receipt.run_id != event.run_id
        or receipt.tool_name != "device.sms.send"
        or receipt.status != DeviceInvocationStatus.SENT
        or receipt.resolved_at is None
        or not receipt.created_at <= receipt.resolved_at <= event.created_at
        or not isinstance(recipient, str)
    ):
        return False
    try:
        match = await resolve_identity(
            uow.people,
            principal,
            kind="phone",
            namespace="owner",
            value=recipient,
            context="owner",
            at=receipt.resolved_at,
            ceiling=Sensitivity.SENSITIVE,
        )
    except ValueError:
        return False
    if match.status != "matched":
        return False
    sid = source_id(principal, event.session_id, event.sequence)
    interaction_id = uuid5(receipt.id, "people-sms-sent@1")
    if (
        await uow.people.source_suppressed(principal, sid)
        or await uow.people.is_erased(principal, interaction_id)
        or await uow.people.get(principal, interaction_id, ceiling=Sensitivity.RESTRICTED)
    ):
        return False
    common: _Common = {
        "tenant_id": principal.tenant_id,
        "principal_id": principal.principal_id,
        "created_at": now,
        "updated_at": now,
        "sensitivity": Sensitivity.SENSITIVE,
    }
    if await uow.people.get(principal, sid, ceiling=Sensitivity.RESTRICTED) is None:
        await uow.people.put(
            PeopleSource(
                id=sid,
                session_id=event.session_id,
                event_sequence=event.sequence,
                source_kind="sms",
                evidence_at=receipt.resolved_at,
                source_revision=hashlib.sha256(
                    receipt.id.bytes + receipt.resolved_at.isoformat().encode()
                ).hexdigest(),
                **common,
            ),
            expected_revision=0,
        )
    await uow.people.put(
        PeopleInteraction(
            id=interaction_id,
            channel="sms",
            interaction_kind="exchange",
            attribution="observed",
            direction="outgoing",
            summary="Sent SMS",
            occurred_at=receipt.resolved_at,
            precision="instant",
            participants=[InteractionParticipant(person_id=match.person_ids[0], role="recipient")],
            support_ids=[sid],
            **common,
        ),
        expected_revision=0,
    )
    return True
