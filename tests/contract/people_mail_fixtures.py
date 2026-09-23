"""Mail read by the owner's mailbox, for correspondence and repair tests (ADR-0118)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from agent_core.domain.email_people import EmailPeopleFact
from agent_core.domain.memory import BeliefType, Sensitivity
from agent_core.domain.people import PeopleQuery
from agent_core.domain.people_extraction import PeopleClaim, PersonEvidence, RelationshipProposal
from agent_core.memory.email_people import EmailPeopleFormationService
from tests.contract.memory_fixtures import semantic_stack
from tests.contract.people_fixtures import PeopleFields
from tests.contract.support import NOW, ids, principal


async def seed_account(
    factory: Any, *, status: str = "syncing", address: str = "owner@example.test"
) -> None:
    from agent_core.domain.email import EmailAccount, EmailRecord

    common: PeopleFields = {
        "tenant_id": principal().tenant_id,
        "principal_id": principal().principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    account = EmailAccount(id="work", label="Work", email_address=address, status=status)  # type: ignore[arg-type]
    async with factory() as uow:
        await uow.email.put(
            EmailRecord(
                kind="account",
                key="work",
                revision=1,
                payload=account.model_dump(mode="json"),
                **common,
            ),
            expected_revision=0,
        )


async def mail(
    factory: Any,
    *,
    message_id: str,
    sender: str,
    to: str | None = None,
    labels: list[str] | None = None,
    days_ago: float = 3,
    thread_id: str = "t-mail",
    body: str = "Thanks for the update on the board materials.",
) -> Any:
    """Append one first-party thread read to the mailbox session and return its source."""
    import json
    from datetime import UTC, datetime
    from uuid import UUID

    from agent_core.domain.email_semantics import EmailSemanticSource
    from agent_core.domain.events import NewEvent
    from agent_core.domain.messages import TextPart, ToolResultItem
    from agent_core.domain.policies import TrustLevel

    sent_at = (NOW - timedelta(days=days_ago)).replace(microsecond=0)
    message: dict[str, object] = {
        "id": message_id,
        "thread_id": thread_id,
        "from": sender,
        "body": body,
        "body_complete": True,
        "headers_complete": True,
        "internal_date": str(int(sent_at.timestamp() * 1000)),
        "label_ids": labels or ["INBOX"],
    }
    if to is not None:
        message["to"] = to
    async with factory() as uow:
        event = await uow.events.append(
            NewEvent(
                session_id=UUID(int=410),
                run_id=None,
                event_type="tool.call.completed",
                actor_type="runtime",
                payload={
                    "name": "mcp.gmail_work_read.get_thread_page",
                    "result_item": ToolResultItem(
                        call_id=f"call-{message_id}",
                        trust=TrustLevel.EXTERNAL_UNTRUSTED,
                        content=[
                            TextPart(
                                text=json.dumps({"thread_id": thread_id, "messages": [message]})
                            )
                        ],
                    ).model_dump(mode="json"),
                },
            )
        )
    return EmailSemanticSource(
        account_id="work",
        provider_thread_id=thread_id,
        message_id=message_id,
        session_id=UUID(int=410),
        source_event_sequence=event.sequence,
        tool_name="mcp.gmail_work_read.get_thread_page",
        sender=sender,
        body=body,
        sent_at=datetime.fromtimestamp(int(str(message["internal_date"])) / 1000, tz=UTC),
    )


def all_rows(kinds: list[str]) -> PeopleQuery:
    return PeopleQuery(
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        kinds=kinds,
        sensitivity_ceiling=Sensitivity.RESTRICTED,
        limit=100,
    )


async def correspondence_stack() -> tuple[Any, EmailPeopleFormationService]:
    factory, legacy, _source, _fact, _ = await semantic_stack(age=30)
    service = EmailPeopleFormationService(
        factory, legacy._clock, ids(), principal(), provider="fake", model="scripted"
    )
    return factory, service


OWNER = "Owner Name <owner@example.test>"
ALEX = "Alex Rivera <alex@example.test>"


def colleague_fact(source: Any, *, maya_label: str = "Maya") -> EmailPeopleFact:
    mentions = [
        PersonEvidence(
            key=name.casefold(),
            source_event_id=1,
            start=source.body.index(name),
            end=source.body.index(name) + len(name),
            text=name,
            display_name=label,
            identifier_kind="name",
            identifier_value=name,
            namespace="owner",
            context="",
            role="subject" if role == "subject" else "object",
            referent_key=None,
        )
        for name, role, label in (("Alex", "subject", "Alex"), ("Maya", "object", maya_label))
    ]
    return EmailPeopleFact(
        message_id=source.message_id,
        quote=source.body,
        belief_type=BeliefType.RELATIONSHIP,
        subject="Alex relationship",
        predicate="is",
        value="Maya's colleague",
        people=PeopleClaim(
            mentions=mentions,
            organizations=[],
            relationship=RelationshipProposal(
                subject_key="alex",
                object_key="maya",
                predicate="colleague",
                qualifier="",
                valid_from=None,
                valid_to=None,
                precision="unknown",
                source_timezone=None,
            ),
            commitment=None,
        ),
    )
